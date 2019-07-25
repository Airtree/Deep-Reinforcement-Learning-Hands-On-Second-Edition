#!/usr/bin/env python3
import gym
import ptan
from datetime import datetime, timedelta
import argparse

import torch
import torch.optim as optim

from ignite.engine import Engine
from ignite.metrics import RunningAverage
from ignite.contrib.handlers import tensorboard_logger as tb_logger

from lib import dqn_model, common, ignite


def batch_generator(buffer: ptan.experience.ExperienceReplayBuffer,
                    initial: int, batch_size: int):
    buffer.populate(initial)
    while True:
        buffer.populate(1)
        yield buffer.sample(batch_size)


if __name__ == "__main__":
    params = common.HYPERPARAMS['pong']
    parser = argparse.ArgumentParser()
    parser.add_argument("--cuda", default=False, action="store_true", help="Enable cuda")
    args = parser.parse_args()
    device = torch.device("cuda" if args.cuda else "cpu")

    env = gym.make(params.env_name)
    env = ptan.common.wrappers.wrap_dqn(env)

    net = dqn_model.DQN(env.observation_space.shape, env.action_space.n).to(device)

    tgt_net = ptan.agent.TargetNet(net)
    selector = ptan.actions.EpsilonGreedyActionSelector(epsilon=params.epsilon_start)
    epsilon_tracker = common.EpsilonTracker(selector, params)
    agent = ptan.agent.DQNAgent(net, selector, device=device)

    exp_source = ptan.experience.ExperienceSourceFirstLast(
        env, agent, gamma=params.gamma, steps_count=1)
    buffer = ptan.experience.ExperienceReplayBuffer(
        exp_source, buffer_size=params.replay_size)
    optimizer = optim.Adam(net.parameters(), lr=params.learning_rate)

    def process_batch(engine, batch):
        optimizer.zero_grad()
        loss_v = common.calc_loss_dqn(batch, net, tgt_net.target_model,
                                      gamma=params.gamma, device=device)
        loss_v.backward()
        optimizer.step()
        epsilon_tracker.frame(engine.state.iteration)
        if engine.state.iteration % params.target_net_sync == 0:
            tgt_net.sync()
        return {
            "loss": loss_v.item(),
            "epsilon": selector.epsilon,
        }

    engine = Engine(process_batch)
    ignite.EndOfEpisodeHandler(exp_source, bound_avg_reward=17.0).attach(engine)
    ignite.EpisodeFPSHandler().attach(engine)

    @engine.on(ignite.EndOfEpisodeHandler.Events.EPISODE_COMPLETED)
    def episode_completed(trainer: Engine):
        print("Episode %d: reward=%s, steps=%s, speed=%.3f frames/s" % (
            trainer.state.episode, trainer.state.episode_reward,
            trainer.state.episode_steps, trainer.state.metrics.get('fps', 0)))

    @engine.on(ignite.EndOfEpisodeHandler.Events.BOUND_REWARD_REACHED)
    def game_solved(trainer: Engine):
        print("Game solved in %s, after %d episodes and %d iterations!" % (
            timedelta(seconds=trainer.state.metrics['time_passed']),
            trainer.state.episode, trainer.state.iteration))
        trainer.should_terminate = True

    logdir = f"runs/{datetime.now().isoformat(timespec='minutes')}-{params.run_name}-01_original"
    tb = tb_logger.TensorboardLogger(log_dir=logdir)
    RunningAverage(output_transform=lambda v: v['loss']).attach(engine, "avg_loss")

    episode_handler = tb_logger.OutputHandler(tag="episodes", metric_names=['reward', 'steps', 'avg_reward'])
    tb.attach(engine, log_handler=episode_handler, event_name=ignite.EndOfEpisodeHandler.Events.EPISODE_COMPLETED)

    # write to tensorboard every 100 iterations
    ignite.PeriodicEvents().attach(engine)
    handler = tb_logger.OutputHandler(tag="train", metric_names=['avg_loss', 'fps'],
                                      output_transform=lambda a: a)
    tb.attach(engine, log_handler=handler, event_name=ignite.PeriodicEvents.Events.ITERATIONS_100_COMPLETED)

    engine.run(batch_generator(buffer, params.replay_initial, params.batch_size))