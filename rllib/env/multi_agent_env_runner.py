from collections import defaultdict
from functools import partial
import logging
from typing import Any, Container, DefaultDict, Dict, List, Optional

import gymnasium as gym

from ray.rllib.algorithms.algorithm_config import AlgorithmConfig
from ray.rllib.algorithms.callbacks import DefaultCallbacks
from ray.rllib.core.columns import Columns
from ray.rllib.core.rl_module.marl_module import MultiAgentRLModuleSpec
from ray.rllib.env.env_context import EnvContext
from ray.rllib.env.env_runner import EnvRunner
from ray.rllib.env.multi_agent_env import MultiAgentEnv
from ray.rllib.env.multi_agent_episode import MultiAgentEpisode
from ray.rllib.env.utils import _gym_env_creator
from ray.rllib.utils import force_list
from ray.rllib.utils.annotations import override
from ray.rllib.utils.deprecation import Deprecated
from ray.rllib.utils.metrics import (
    EPISODE_DURATION_SEC_MEAN,
    EPISODE_LEN_MAX,
    EPISODE_LEN_MEAN,
    EPISODE_LEN_MIN,
    EPISODE_RETURN_MAX,
    EPISODE_RETURN_MEAN,
    EPISODE_RETURN_MIN,
    NUM_AGENT_STEPS_SAMPLED,
    NUM_AGENT_STEPS_SAMPLED_LIFETIME,
    NUM_ENV_STEPS_SAMPLED,
    NUM_ENV_STEPS_SAMPLED_LIFETIME,
    NUM_EPISODES,
    NUM_MODULE_STEPS_SAMPLED,
    NUM_MODULE_STEPS_SAMPLED_LIFETIME,
    WEIGHTS_SEQ_NO,
)
from ray.rllib.utils.metrics.metrics_logger import MetricsLogger
from ray.rllib.utils.pre_checks.env import check_multiagent_environments
from ray.rllib.utils.typing import EpisodeID, ModelWeights, ResultDict
from ray.util.annotations import PublicAPI
from ray.tune.registry import ENV_CREATOR, _global_registry

logger = logging.getLogger("ray.rllib")


@PublicAPI(stability="alpha")
class MultiAgentEnvRunner(EnvRunner):
    """The genetic environment runner for the multi-agent case."""

    @override(EnvRunner)
    def __init__(self, config: AlgorithmConfig, **kwargs):
        """Initializes a MultiAgentEnvRunner instance.

        Args:
            config: An `AlgorithmConfig` object containing all settings needed to
                build this `EnvRunner` class.
        """
        super().__init__(config=config)

        # Raise an Error, if the provided config is not a multi-agent one.
        if not self.config.is_multi_agent():
            raise ValueError(
                f"Cannot use this EnvRunner class ({type(self).__name__}), if your "
                "setup is not multi-agent! Try adding multi-agent information to your "
                "AlgorithmConfig via calling the `config.multi_agent(policies=..., "
                "policy_mapping_fn=...)`."
            )

        # Get the worker index on which this instance is running.
        self.worker_index: int = kwargs.get("worker_index")

        # Set up all metrics-related structures and counters.
        self.metrics: Optional[MetricsLogger] = None
        self._setup_metrics()

        # Create our callbacks object.
        self._callbacks: DefaultCallbacks = self.config.callbacks_class()

        # Create the vectorized gymnasium env.
        self.env: Optional[gym.Wrapper] = None
        self.num_envs: int = 0
        self.make_env()

        # Create the env-to-module connector pipeline.
        self._env_to_module = self.config.build_env_to_module_connector(self.env)
        # Cached env-to-module results taken at the end of a `_sample_timesteps()`
        # call to make sure the final observation (before an episode cut) gets properly
        # processed (and maybe postprocessed and re-stored into the episode).
        # For example, if we had a connector that normalizes observations and directly
        # re-inserts these new obs back into the episode, the last observation in each
        # sample call would NOT be processed, which could be very harmful in cases,
        # in which value function bootstrapping of those (truncation) observations is
        # required in the learning step.
        self._cached_to_module = None

        # Construct the RLModule.
        self.module = self._make_module()

        # Create the two connector pipelines: env-to-module and module-to-env.
        self._module_to_env = self.config.build_module_to_env_connector(self.env)

        self._needs_initial_reset: bool = True
        self._episode: Optional[MultiAgentEpisode] = None
        self._shared_data = None

        self._weights_seq_no: int = 0

    @override(EnvRunner)
    def sample(
        self,
        *,
        num_timesteps: int = None,
        num_episodes: int = None,
        explore: bool = None,
        random_actions: bool = False,
        force_reset: bool = False,
    ) -> List[MultiAgentEpisode]:
        """Runs and returns a sample (n timesteps or m episodes) on the env(s).

        Args:
            num_timesteps: The number of timesteps to sample during this call.
                Note that only one of `num_timetseps` or `num_episodes` may be provided.
            num_episodes: The number of episodes to sample during this call.
                Note that only one of `num_timetseps` or `num_episodes` may be provided.
            explore: If True, will use the RLModule's `forward_exploration()`
                method to compute actions. If False, will use the RLModule's
                `forward_inference()` method. If None (default), will use the `explore`
                boolean setting from `self.config` passed into this EnvRunner's
                constructor. You can change this setting in your config via
                `config.env_runners(explore=True|False)`.
            random_actions: If True, actions will be sampled randomly (from the action
                space of the environment). If False (default), actions or action
                distribution parameters are computed by the RLModule.
            force_reset: Whether to force-reset all (vector) environments before
                sampling. Useful if you would like to collect a clean slate of new
                episodes via this call. Note that when sampling n episodes
                (`num_episodes != None`), this is fixed to True.

        Returns:
            A list of `MultiAgentEpisode` instances, carrying the sampled data.
        """
        assert not (num_timesteps is not None and num_episodes is not None)

        # If no execution details are provided, use the config to try to infer the
        # desired timesteps/episodes to sample and the exploration behavior.
        if explore is None:
            explore = self.config.explore
        if num_timesteps is None and num_episodes is None:
            if self.config.batch_mode == "truncate_episodes":
                num_timesteps = self.config.get_rollout_fragment_length(
                    worker_index=self.worker_index,
                )
            else:
                num_episodes = 1

        # Sample n timesteps.
        if num_timesteps is not None:
            samples = self._sample_timesteps(
                num_timesteps=num_timesteps,
                explore=explore,
                random_actions=random_actions,
                force_reset=force_reset,
            )
        # Sample m episodes.
        else:
            samples = self._sample_episodes(
                num_episodes=num_episodes,
                explore=explore,
                random_actions=random_actions,
            )

        # Make the `on_sample_end` callback.
        self._callbacks.on_sample_end(
            env_runner=self,
            metrics_logger=self.metrics,
            samples=samples,
        )

        return samples

    def _sample_timesteps(
        self,
        num_timesteps: int,
        explore: bool,
        random_actions: bool = False,
        force_reset: bool = False,
    ) -> List[MultiAgentEpisode]:
        """Helper method to sample n timesteps.

        Args:
            num_timesteps: int. Number of timesteps to sample during rollout.
            explore: boolean. If in exploration or inference mode. Exploration
                mode might for some algorithms provide extza model outputs that
                are redundant in inference mode.
            random_actions: boolean. If actions should be sampled from the action
                space. In default mode (i.e. `False`) we sample actions frokm the
                policy.

        Returns:
            `Lists of `MultiAgentEpisode` instances, carrying the collected sample data.
        """
        done_episodes_to_return: List[MultiAgentEpisode] = []

        # Have to reset the env.
        if force_reset or self._needs_initial_reset:
            # Create n new episodes and make the `on_episode_created` callbacks.
            self._episode = self._new_episode()
            self._make_on_episode_callback("on_episode_created")

            # Erase all cached ongoing episodes (these will never be completed and
            # would thus never be returned/cleaned by `get_metrics` and cause a memory
            # leak).
            self._ongoing_episodes_for_metrics.clear()

            # Reset the environment.
            # TODO (simon): Check, if we need here the seed from the config.
            obs, infos = self.env.reset()
            self._cached_to_module = None

            # Call `on_episode_start()` callbacks.
            self._make_on_episode_callback("on_episode_start")

            # We just reset the env. Don't have to force this again in the next
            # call to `self._sample_timesteps()`.
            self._needs_initial_reset = False

            # Set the initial observations in the episodes.
            self._episode.add_env_reset(observations=obs, infos=infos)

            self._shared_data = {
                "agent_to_module_mapping_fn": self.config.policy_mapping_fn,
            }

        # Loop through timesteps.
        ts = 0

        while ts < num_timesteps:
            # Act randomly.
            if random_actions:
                # Note, to get sampled actions from all agents' action
                # spaces we need to call `MultiAgentEnv.action_space_sample()`.
                if self.env.unwrapped._action_space_in_preferred_format:
                    actions = self.env.action_space.sample()
                # Otherwise, `action_space_sample()` needs to be implemented.
                else:
                    actions = self.env.action_space_sample()
                # Remove all actions for agents that had no observation.
                to_env = {
                    Columns.ACTIONS: [
                        {
                            agent_id: agent_action
                            for agent_id, agent_action in actions.items()
                            if agent_id in self._episode.get_agents_to_act()
                        }
                    ]
                }
            # Compute an action using the RLModule.
            else:
                # Env-to-module connector.
                to_module = self._cached_to_module or self._env_to_module(
                    rl_module=self.module,
                    episodes=[self._episode],
                    explore=explore,
                    shared_data=self._shared_data,
                )
                self._cached_to_module = None

                # MARLModule forward pass: Explore or not.
                if explore:
                    env_steps_lifetime = self.metrics.peek(
                        NUM_ENV_STEPS_SAMPLED_LIFETIME, default=0
                    ) + self.metrics.peek(NUM_ENV_STEPS_SAMPLED, default=0)
                    to_env = self.module.forward_exploration(
                        to_module, t=env_steps_lifetime
                    )
                else:
                    to_env = self.module.forward_inference(to_module)

                # Module-to-env connector.
                to_env = self._module_to_env(
                    rl_module=self.module,
                    data=to_env,
                    episodes=[self._episode],
                    explore=explore,
                    shared_data=self._shared_data,
                )

            # Extract the (vectorized) actions (to be sent to the env) from the
            # module/connector output. Note that these actions are fully ready (e.g.
            # already unsquashed/clipped) to be sent to the environment) and might not
            # be identical to the actions produced by the RLModule/distribution, which
            # are the ones stored permanently in the episode objects.
            actions = to_env.pop(Columns.ACTIONS)
            actions_for_env = to_env.pop(Columns.ACTIONS_FOR_ENV, actions)
            # Step the environment.
            # TODO (sven): [0] = actions is vectorized, but env is NOT a vector Env.
            #  Support vectorized multi-agent envs.
            obs, rewards, terminateds, truncateds, infos = self.env.step(
                actions_for_env[0]
            )
            ts += self._increase_sampled_metrics(self.num_envs, obs, self._episode)

            # TODO (sven): This simple approach to re-map `to_env` from a
            #  dict[col, List[MADict]] to a dict[agentID, MADict] would not work for
            #  a vectorized env.
            extra_model_outputs = defaultdict(dict)
            for col, ma_dict_list in to_env.items():
                # TODO (sven): Support vectorized MA env.
                ma_dict = ma_dict_list[0]
                for agent_id, val in ma_dict.items():
                    extra_model_outputs[agent_id][col] = val
            extra_model_outputs = dict(extra_model_outputs)

            # Record the timestep in the episode instance.
            self._episode.add_env_step(
                obs,
                actions[0],
                rewards,
                infos=infos,
                terminateds=terminateds,
                truncateds=truncateds,
                extra_model_outputs=extra_model_outputs,
            )

            # Make the `on_episode_step` callback (before finalizing the episode
            # object).
            self._make_on_episode_callback("on_episode_step")

            # Episode is done for all agents. Wrap up the old one and create a new
            # one (and reset it) to continue.
            if self._episode.is_done:
                # We have to perform an extra env-to-module pass here, just in case
                # the user's connector pipeline performs (permanent) transforms
                # on each observation (including this final one here). Without such
                # a call and in case the structure of the observations change
                # sufficiently, the following `finalize()` call on the episode will
                # fail.
                if self.module is not None:
                    self._env_to_module(
                        episodes=[self._episode],
                        explore=explore,
                        rl_module=self.module,
                        shared_data=self._shared_data,
                    )

                # Make the `on_episode_end` callback (before finalizing the episode,
                # but after(!) the last env-to-module connector call has been made.
                # -> All obs (even the terminal one) should have been processed now (by
                # the connector, if applicable).
                self._make_on_episode_callback("on_episode_end")

                # Finalize (numpy'ize) the episode.
                self._episode.finalize(drop_zero_len_single_agent_episodes=True)
                done_episodes_to_return.append(self._episode)

                # Create a new episode instance.
                self._episode = self._new_episode()
                self._make_on_episode_callback("on_episode_created")

                # Reset the environment.
                obs, infos = self.env.reset()
                # Add initial observations and infos.
                self._episode.add_env_reset(observations=obs, infos=infos)

                # Make the `on_episode_start` callback.
                self._make_on_episode_callback("on_episode_start")

        # Already perform env-to-module connector call for next call to
        # `_sample_timesteps()`. See comment in c'tor for `self._cached_to_module`.
        if self.module is not None:
            self._cached_to_module = self._env_to_module(
                rl_module=self.module,
                episodes=[self._episode],
                explore=explore,
                shared_data=self._shared_data,
            )

        # Store done episodes for metrics.
        self._done_episodes_for_metrics.extend(done_episodes_to_return)

        # Also, make sure we start new episode chunks (continuing the ongoing episodes
        # from the to-be-returned chunks).
        ongoing_episode_continuation = self._episode.cut(
            len_lookback_buffer=self.config.episode_lookback_horizon
        )

        ongoing_episodes_to_return = []
        # Just started Episodes do not have to be returned. There is no data
        # in them anyway.
        if self._episode.env_t > 0:
            self._episode.validate()
            self._ongoing_episodes_for_metrics[self._episode.id_].append(self._episode)
            # Return finalized (numpy'ized) Episodes.
            ongoing_episodes_to_return.append(
                self._episode.finalize(drop_zero_len_single_agent_episodes=True)
            )

        # Continue collecting into the cut Episode chunk.
        self._episode = ongoing_episode_continuation

        # Return collected episode data.
        return done_episodes_to_return + ongoing_episodes_to_return

    def _sample_episodes(
        self,
        num_episodes: int,
        explore: bool,
        random_actions: bool = False,
    ) -> List[MultiAgentEpisode]:
        """Helper method to run n episodes.

        See docstring of `self.sample()` for more details.
        """
        # If user calls sample(num_timesteps=..) after this, we must reset again
        # at the beginning.
        self._needs_initial_reset = True

        done_episodes_to_return: List[MultiAgentEpisode] = []

        # Create a new multi-agent episode.
        _episode = self._new_episode()
        self._make_on_episode_callback("on_episode_created", _episode)
        _shared_data = {
            "agent_to_module_mapping_fn": self.config.policy_mapping_fn,
        }

        # Reset the environment.
        # TODO (simon): Check, if we need here the seed from the config.
        obs, infos = self.env.reset()
        # Set initial obs and infos in the episodes.
        _episode.add_env_reset(observations=obs, infos=infos)
        self._make_on_episode_callback("on_episode_start", _episode)

        # Loop over episodes.
        eps = 0
        ts = 0
        while eps < num_episodes:
            # Act randomly.
            if random_actions:
                # Note, to get sampled actions from all agents' action
                # spaces we need to call `MultiAgentEnv.action_space_sample()`.
                if self.env.unwrapped._action_space_in_preferred_format:
                    actions = self.env.action_space.sample()
                # Otherwise, `action_space_sample()` needs to be implemented.
                else:
                    actions = self.env.action_space_sample()
                # Remove all actions for agents that had no observation.
                to_env = {
                    Columns.ACTIONS: {
                        agent_id: agent_action
                        for agent_id, agent_action in actions.items()
                        if agent_id in _episode.get_agents_to_act()
                    },
                }
            # Compute an action using the RLModule.
            else:
                # Env-to-module connector.
                to_module = self._env_to_module(
                    rl_module=self.module,
                    episodes=[_episode],
                    explore=explore,
                    shared_data=_shared_data,
                )

                # MARLModule forward pass: Explore or not.
                if explore:
                    env_steps_lifetime = self.metrics.peek(
                        NUM_ENV_STEPS_SAMPLED_LIFETIME, default=0
                    ) + self.metrics.peek(NUM_ENV_STEPS_SAMPLED, default=0)
                    to_env = self.module.forward_exploration(
                        to_module, t=env_steps_lifetime
                    )
                else:
                    to_env = self.module.forward_inference(to_module)

                # Module-to-env connector.
                to_env = self._module_to_env(
                    rl_module=self.module,
                    data=to_env,
                    episodes=[_episode],
                    explore=explore,
                    shared_data=_shared_data,
                )

            # Extract the (vectorized) actions (to be sent to the env) from the
            # module/connector output. Note that these actions are fully ready (e.g.
            # already unsquashed/clipped) to be sent to the environment) and might not
            # be identical to the actions produced by the RLModule/distribution, which
            # are the ones stored permanently in the episode objects.
            actions = to_env.pop(Columns.ACTIONS)
            actions_for_env = to_env.pop(Columns.ACTIONS_FOR_ENV, actions)
            # Step the environment.
            # TODO (sven): [0] = actions is vectorized, but env is NOT a vector Env.
            #  Support vectorized multi-agent envs.
            obs, rewards, terminateds, truncateds, infos = self.env.step(
                actions_for_env[0]
            )
            ts += self._increase_sampled_metrics(self.num_envs, obs, _episode)

            # TODO (sven): This simple approach to re-map `to_env` from a
            #  dict[col, List[MADict]] to a dict[agentID, MADict] would not work for
            #  a vectorized env.
            extra_model_outputs = defaultdict(dict)
            for col, ma_dict_list in to_env.items():
                # TODO (sven): Support vectorized MA env.
                ma_dict = ma_dict_list[0]
                for agent_id, val in ma_dict.items():
                    extra_model_outputs[agent_id][col] = val
            extra_model_outputs = dict(extra_model_outputs)

            # Record the timestep in the episode instance.
            _episode.add_env_step(
                obs,
                actions[0],
                rewards,
                infos=infos,
                terminateds=terminateds,
                truncateds=truncateds,
                extra_model_outputs=extra_model_outputs,
            )

            # Make `on_episode_step` callback before finalizing the episode.
            self._make_on_episode_callback("on_episode_step", _episode)

            # TODO (sven, simon): We have to check, if we need this elaborate
            # function here or if the `MultiAgentEnv` defines the cases that
            # can happen.
            # Right now we have:
            #   1. Most times only agents that step get `terminated`, `truncated`
            #       i.e. the rest we have to check in the episode.
            #   2. There are edge cases like, some agents terminated, all others
            #       truncated and vice versa.
            # See also `MultiAgentEpisode` for handling the `__all__`.
            if _episode.is_done:
                # Increase episode count.
                eps += 1

                # We have to perform an extra env-to-module pass here, just in case
                # the user's connector pipeline performs (permanent) transforms
                # on each observation (including this final one here). Without such
                # a call and in case the structure of the observations change
                # sufficiently, the following `finalize()` call on the episode will
                # fail.
                if self.module is not None:
                    self._env_to_module(
                        episodes=[_episode],
                        explore=explore,
                        rl_module=self.module,
                        shared_data=_shared_data,
                    )

                # Make the `on_episode_end` callback (before finalizing the episode,
                # but after(!) the last env-to-module connector call has been made.
                # -> All obs (even the terminal one) should have been processed now (by
                # the connector, if applicable).
                self._make_on_episode_callback("on_episode_end", _episode)

                # Finish the episode.
                done_episodes_to_return.append(
                    _episode.finalize(drop_zero_len_single_agent_episodes=True)
                )

                # Also early-out if we reach the number of episodes within this
                # for-loop.
                if eps == num_episodes:
                    break

                # Create a new episode instance.
                _episode = self._new_episode()
                self._make_on_episode_callback("on_episode_created", _episode)

                # Reset the environment.
                obs, infos = self.env.reset()
                # Add initial observations and infos.
                _episode.add_env_reset(observations=obs, infos=infos)

                # Make `on_episode_start` callback.
                self._make_on_episode_callback("on_episode_start", _episode)

        self._done_episodes_for_metrics.extend(done_episodes_to_return)

        return done_episodes_to_return

    def get_metrics(self) -> ResultDict:
        # Compute per-episode metrics (only on already completed episodes).
        for eps in self._done_episodes_for_metrics:
            assert eps.is_done
            episode_length = len(eps)
            agent_steps = defaultdict(
                int,
                {str(aid): len(sa_eps) for aid, sa_eps in eps.agent_episodes.items()},
            )
            episode_return = eps.get_return()
            episode_duration_s = eps.get_duration_s()

            agent_episode_returns = defaultdict(
                float,
                {
                    str(sa_eps.agent_id): sa_eps.get_return()
                    for sa_eps in eps.agent_episodes.values()
                },
            )
            module_episode_returns = defaultdict(
                float,
                {
                    sa_eps.module_id: sa_eps.get_return()
                    for sa_eps in eps.agent_episodes.values()
                },
            )

            # Don't forget about the already returned chunks of this episode.
            if eps.id_ in self._ongoing_episodes_for_metrics:
                for eps2 in self._ongoing_episodes_for_metrics[eps.id_]:
                    return_eps2 = eps2.get_return()
                    episode_length += len(eps2)
                    episode_return += return_eps2
                    episode_duration_s += eps2.get_duration_s()

                    for sa_eps in eps2.agent_episodes.values():
                        return_sa = sa_eps.get_return()
                        agent_steps[str(sa_eps.agent_id)] += len(sa_eps)
                        agent_episode_returns[str(sa_eps.agent_id)] += return_sa
                        module_episode_returns[sa_eps.module_id] += return_sa

                del self._ongoing_episodes_for_metrics[eps.id_]

            self._log_episode_metrics(
                episode_length,
                episode_return,
                episode_duration_s,
                agent_episode_returns,
                module_episode_returns,
                dict(agent_steps),
            )

        # Log num episodes counter for this iteration.
        self.metrics.log_value(
            NUM_EPISODES,
            len(self._done_episodes_for_metrics),
            reduce="sum",
            clear_on_reduce=True,  # Not a lifetime count.
        )

        # Now that we have logged everything, clear cache of done episodes.
        self._done_episodes_for_metrics.clear()

        # Return reduced metrics.
        return self.metrics.reduce()

    @override(EnvRunner)
    def get_state(
        self,
        components: Optional[Container[str]] = None,
        *,
        inference_only: bool = True,
        module_ids=None,
    ) -> Dict[str, Any]:
        components = force_list(
            components
            if components is not None
            else ["rl_module", "env_to_module_connector", "module_to_env_connector"]
        )
        state = {
            WEIGHTS_SEQ_NO: self._weights_seq_no,
            NUM_ENV_STEPS_SAMPLED_LIFETIME: (
                self.metrics.peek(NUM_ENV_STEPS_SAMPLED_LIFETIME, default=0)
            ),
        }
        if "rl_module" in components:
            state["rl_module"] = self.module.get_state(
                inference_only=inference_only, module_ids=module_ids
            )
        if "env_to_module_connector" in components:
            state["env_to_module_connector"] = self._env_to_module.get_state()
        if "module_to_env_connector" in components:
            state["module_to_env_connector"] = self._module_to_env.get_state()

        return state

    @override(EnvRunner)
    def set_state(self, state: Dict[str, Any]) -> None:
        if "env_to_module_connector" in state:
            self._env_to_module.set_state(state["env_to_module_connector"])
        if "module_to_env_connector" in state:
            self._module_to_env.set_state(state["module_to_env_connector"])

        # Update the RLModule state.
        if "rl_module" in state:
            # A missing value for WEIGHTS_SEQ_NO or a value of 0 means: Force the
            # update.
            weights_seq_no = state.get(WEIGHTS_SEQ_NO, 0)

            # Only update the weigths, if this is the first synchronization or
            # if the weights of this `EnvRunner` lacks behind the actual ones.
            if weights_seq_no == 0 or self._weights_seq_no < weights_seq_no:
                weights = state["rl_module"]
                weights = self._convert_to_tensor(weights)
                self.module.set_state(weights)

            # Update our weights_seq_no, if the new one is > 0.
            if weights_seq_no > 0:
                self._weights_seq_no = weights_seq_no

        # Update our lifetime counters.
        if NUM_ENV_STEPS_SAMPLED_LIFETIME in state:
            self.metrics.set_value(
                key=NUM_ENV_STEPS_SAMPLED_LIFETIME,
                value=state[NUM_ENV_STEPS_SAMPLED_LIFETIME],
                reduce="sum",
            )

    @override(EnvRunner)
    def assert_healthy(self):
        """Checks that self.__init__() has been completed properly.

        Ensures that the instances has a `MultiAgentRLModule` and an
        environment defined.

        Raises:
            AssertionError: If the EnvRunner Actor has NOT been properly initialized.
        """
        # Make sure, we have built our gym.vector.Env and RLModule properly.
        assert self.env and self.module

    def make_env(self):
        """Creates a MultiAgentEnv (is-a gymnasium env).

        Note that users can change the EnvRunner's config (e.g. change
        `self.config.env_config`) and then call this method to create new environments
        with the updated configuration.
        """
        # If an env already exists, try closing it first (to allow it to properly
        # cleanup).
        if self.env is not None:
            try:
                self.env.close()
            except Exception as e:
                logger.warning(
                    "Tried closing the existing env (multi-agent), but failed with "
                    f"error: {e.args[0]}"
                )

        env_ctx = self.config.env_config
        if not isinstance(env_ctx, EnvContext):
            env_ctx = EnvContext(
                env_ctx,
                worker_index=self.worker_index,
                num_workers=self.config.num_env_runners,
                remote=self.config.remote_worker_envs,
            )

        # Register env for the local context.
        # Note, `gym.register` has to be called on each worker.
        if isinstance(self.config.env, str) and _global_registry.contains(
            ENV_CREATOR, self.config.env
        ):
            entry_point = partial(
                _global_registry.get(ENV_CREATOR, self.config.env),
                env_ctx,
            )

        else:
            entry_point = partial(
                _gym_env_creator,
                env_descriptor=self.config.env,
                env_context=env_ctx,
            )
        gym.register(
            "rllib-multi-agent-env-v0",
            entry_point=entry_point,
            disable_env_checker=True,
        )

        # Perform actual gym.make call.
        self.env: MultiAgentEnv = gym.make("rllib-multi-agent-env-v0")
        try:
            check_multiagent_environments(self.env.unwrapped)
        except Exception as e:
            logger.exception(e.args[0])
        self.num_envs = 1

        # Create the MultiAgentEnv (is-a gymnasium env).
        assert isinstance(self.env.unwrapped, MultiAgentEnv), (
            "ERROR: When using the `MultiAgentEnvRunner` the environment needs "
            "to inherit from `ray.rllib.env.multi_agent_env.MultiAgentEnv`."
        )

        # Set the flag to reset all envs upon the next `sample()` call.
        self._needs_initial_reset = True

        # Call the `on_environment_created` callback.
        self._callbacks.on_environment_created(
            env_runner=self,
            metrics_logger=self.metrics,
            env=self.env,
            env_context=env_ctx,
        )

    @override(EnvRunner)
    def stop(self):
        # Note, `MultiAgentEnv` inherits `close()`-method from `gym.Env`.
        self.env.close()

    def _make_module(self):
        # Create our own instance of the (single-agent) `RLModule` (which
        # the needs to be weight-synched) each iteration.
        # TODO (sven, simon): We have to rebuild the `AlgorithmConfig` to work on
        #  `RLModule`s and not `Policy`s. Like here `policies`->`modules`.
        try:
            policy_dict, _ = self.config.get_multi_agent_setup(
                spaces={
                    mid: (o, self._env_to_module.action_space[mid])
                    for mid, o in self._env_to_module.observation_space.spaces.items()
                },
            )
            ma_rlm_spec: MultiAgentRLModuleSpec = self.config.get_marl_module_spec(
                policy_dict=policy_dict,
                # Built only a light version of the module in sampling and inference.
                inference_only=True,
            )

            # Build the module from its spec.
            return ma_rlm_spec.build()

        # This error could be thrown, when only random actions are used.
        except NotImplementedError:
            return None

    def _setup_metrics(self):
        self.metrics = MetricsLogger()

        self._done_episodes_for_metrics: List[MultiAgentEpisode] = []
        self._ongoing_episodes_for_metrics: DefaultDict[
            EpisodeID, List[MultiAgentEpisode]
        ] = defaultdict(list)

    def _new_episode(self):
        return MultiAgentEpisode(
            observation_space=self.env.observation_space,
            action_space=self.env.action_space,
            agent_to_module_mapping_fn=self.config.policy_mapping_fn,
        )

    def _make_on_episode_callback(self, which: str, episode=None):
        episode = episode if episode is not None else self._episode
        getattr(self._callbacks, which)(
            episode=episode,
            env_runner=self,
            metrics_logger=self.metrics,
            env=self.env,
            rl_module=self.module,
            env_index=0,
        )

    def _increase_sampled_metrics(self, num_steps, next_obs, episode):
        self.metrics.log_value(
            NUM_ENV_STEPS_SAMPLED, num_steps, reduce="sum", clear_on_reduce=True
        )
        self.metrics.log_value(NUM_ENV_STEPS_SAMPLED_LIFETIME, num_steps, reduce="sum")
        # TODO (sven): obs is not-vectorized. Support vectorized MA envs.
        for aid in next_obs:
            self.metrics.log_value(
                (NUM_AGENT_STEPS_SAMPLED, str(aid)),
                1,
                reduce="sum",
                clear_on_reduce=True,
            )
            self.metrics.log_value(
                (NUM_AGENT_STEPS_SAMPLED_LIFETIME, str(aid)),
                1,
                reduce="sum",
            )
            self.metrics.log_value(
                (NUM_MODULE_STEPS_SAMPLED, episode.module_for(aid)),
                1,
                reduce="sum",
                clear_on_reduce=True,
            )
            self.metrics.log_value(
                (NUM_MODULE_STEPS_SAMPLED_LIFETIME, episode.module_for(aid)),
                1,
                reduce="sum",
            )
        return num_steps

    def _log_episode_metrics(
        self,
        length,
        ret,
        sec,
        agents=None,
        modules=None,
        agent_steps=None,
    ):
        # Log general episode metrics.
        self.metrics.log_dict(
            {
                EPISODE_LEN_MEAN: length,
                EPISODE_RETURN_MEAN: ret,
                EPISODE_DURATION_SEC_MEAN: sec,
                **(
                    {
                        # Per-agent returns.
                        "agent_episode_returns_mean": agents,
                        # Per-RLModule returns.
                        "module_episode_returns_mean": modules,
                        "agent_steps": agent_steps,
                    }
                    if agents is not None
                    else {}
                ),
            },
            # To mimick the old API stack behavior, we'll use `window` here for
            # these particular stats (instead of the default EMA).
            window=self.config.metrics_num_episodes_for_smoothing,
        )
        # For some metrics, log min/max as well.
        self.metrics.log_dict(
            {
                EPISODE_LEN_MIN: length,
                EPISODE_RETURN_MIN: ret,
            },
            reduce="min",
            window=self.config.metrics_num_episodes_for_smoothing,
        )
        self.metrics.log_dict(
            {
                EPISODE_LEN_MAX: length,
                EPISODE_RETURN_MAX: ret,
            },
            reduce="max",
            window=self.config.metrics_num_episodes_for_smoothing,
        )

    @Deprecated(
        new="MultiAgentEnvRunner.get_state(components='rl_module')",
        error=False,
    )
    def get_weights(self, modules=None):
        return self.get_state(components="rl_module")["rl_module"]

    @Deprecated(new="MultiAgentEnvRunner.set_state()", error=False)
    def set_weights(
        self,
        weights: ModelWeights,
        global_vars: Optional[Dict] = None,
        weights_seq_no: int = 0,
    ) -> None:
        assert global_vars is None
        return self.set_state(
            {
                "rl_module": weights,
                WEIGHTS_SEQ_NO: weights_seq_no,
            }
        )
