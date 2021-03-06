"""
PySC2_A3C_Agent.py
A script for training and running an A3C agent on the PySC2 environment, with reference to DeepMind's paper:
[1] Vinyals, Oriol, et al. "Starcraft II: A new challenge for reinforcement learning." arXiv preprint arXiv:1708.04782 (2017).
Advantage estimation uses generalized advantage estimation from:
[2] Schulman, John, et al. "High-dimensional continuous control using generalized advantage estimation." arXiv preprint arXiv:1506.02438 (2015).

Credit goes to Arthur Juliani for providing for reference an implementation of A3C for the VizDoom environment
https://medium.com/emergent-future/simple-reinforcement-learning-with-tensorflow-part-8-asynchronous-actor-critic-agents-a3c-c88f72a5e9f2
https://github.com/awjuliani/DeepRL-Agents
"""

import threading
import psutil
import numpy as np
import tensorflow as tf
import scipy.signal
from time import sleep
import os
import sys
from absl import flags
from absl.flags import FLAGS

from pysc2.env import sc2_env
from pysc2.env import environment
from pysc2.lib import actions
from pysc2.maps import mini_games
from pysc2.lib import features
import SC2Definitions

_UNIT_TYPE = features.SCREEN_FEATURES.unit_type.index
_PLAYER_RELATIVE = features.SCREEN_FEATURES.player_relative.index
_ENEMY = 4
_SELECT_SIZE = 7

"""
Use the following command to launch Tensorboard:
tensorboard --logdir=worker_0:'./train_0',worker_1:'./train_1',worker_2:'./train_2',worker_3:'./train_3'
"""


## HELPER FUNCTIONS

# Copies one set of variables to another.
# Used to set worker network parameters to those of global network.
def update_target_graph(from_scope,to_scope):
	from_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, from_scope)
	to_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, to_scope)
	op_holder = []
	for from_var,to_var in zip(from_vars,to_vars):
		op_holder.append(to_var.assign(from_var))
	return op_holder


# Discounting function used to calculate discounted returns.
def discount(x, gamma):
	return scipy.signal.lfilter([1], [1, -gamma], x[::-1], axis=0)[::-1]

# Used to initialize weights for policy and value output layers
def normalized_columns_initializer(std=1.0):
	def _initializer(shape, dtype=None, partition_info=None):
		out = np.random.randn(*shape).astype(np.float32)
		out *= std / np.sqrt(np.square(out).sum(axis=0, keepdims=True))
		return tf.constant(out)
	return _initializer

# Sample from a given distribution
def sample_dist(dist):
	sample = np.random.choice(dist[0],p=dist[0])
	sample = np.argmax(dist == sample)
	return sample

# Structure data of AC Netowirk based on race of player
class AgentModel:
    def __init__(self, race = 'T', is_training = False, screen_size = 128, minimap_size=128, max_episodes_kept = 50, save_increment = 100, agent_model = None):
        if agent_model != None and isinstance(agent_model, AgentModel):
                self.screen_size = agent_model.screen_size
                self.save_increment = agent_model.save_increment
                self.max_episodes_kept = agent_model.max_episodes_kept
                self.minimap_size = agent_model.minimap_size
                self.is_training = agent_model.is_training
                self.race = agent_model.race
        else:
                if race not in sc2_env.races.keys():
                        raise ValueError("Invalid race selected: {0}.\n Race must be one of {1}.".format(race, sc2_env.races.keys()))
                self.screen_size = screen_size
                self.save_increment = save_increment
                self.max_episodes_kept = max_episodes_kept
                self.minimap_size = minimap_size
                self.is_training = is_training
                self.race = race
        self.screen_channels = len(features.SCREEN_FEATURES)
        self.minimap_channels = len(features.MINIMAP_FEATURES)
        self.variable_features = {'cargo': 500, 'multi_select': 500, 'build_queue': 10, 'single_select': 1}
        self.setup_actions()
        self.reset()
        self.nonspatial_size = self.calculate_nonspatial_size()

    def setup_actions(self):
        if len(SC2Definitions.ACTIONS[self.race]) < 1 or len(SC2Definitions.ACTIONS['N']) < 1:
            print('Classifying actions based on race...')
            SC2Definitions.classify_actions()
        self.general_actions = list(SC2Definitions.ACTIONS['N'])
        #Limit actions based on race
        self.race_actions = list(SC2Definitions.ACTIONS[self.race])
        #Create dictionaries for quicker look-ups of action indices
        self.action_indices = {'N':{},self.race:{}}
        for i in range(len(self.general_actions)):
            self.action_indices['N'][self.general_actions[i]] = i
        for i in range(len(self.race_actions)):
            self.action_indices[self.race][self.race_actions[i]] = i
        #Create arrays counting how many times actions were used
        self.used_actions = {'N':np.zeros(len(self.general_actions)), self.race:np.zeros(len(self.race_actions))}
        self.action_count = len(self.general_actions)+len(self.race_actions)
        
        
    def reset(self):
        #Keep track of last action used
        self.last_action_used = 0
        #Keep track of units seen for duration of game
        self.max_units_seen = np.zeros(SC2Definitions.UNIT_TYPES)
        self.used_actions = {'N':np.zeros(len(self.general_actions)), self.race:np.zeros(len(self.race_actions))}
        
    def process_observation(self, observation):
        #Update units seen
        enemy_unit_types = np.zeros(self.max_units_seen.size)
        for i in range(observation.observation['screen'].shape[0]):
            for j in range(observation.observation['screen'].shape[1]):
                if observation.observation['screen'][_PLAYER_RELATIVE, i, j] == _ENEMY and observation.observation['screen'][_UNIT_TYPE, i, j] < SC2Definitions.UNIT_TYPES:
                    enemy_unit_types[observation.observation['screen'][_UNIT_TYPE, i, j]] += 1
        for i in range(1, SC2Definitions.UNIT_TYPES):
            self.max_units_seen[i] = max(self.max_units_seen[i]*3/4, enemy_unit_types[i])
        #Process usable actions
        available_actions = np.zeros(self.action_count)
        for i in range(self.action_count):
                if (self.get_action(i).id in observation.observation['available_actions']):
                        available_actions[i] = 1
        # is episode over?
        episode_end = (observation.step_type == environment.StepType.LAST)
	# reward
        reward = observation.reward
	# features
        features = observation.observation
        spatial_features = ['minimap', 'screen']
        variable_features = ['cargo', 'multi_select', 'build_queue']
        # the shapes of some features depend on the state (eg. shape of multi_select depends on number of units)
        # since tf requires fixed input shapes, we set a maximum size then pad the input if it falls short
        max_no = {'cargo': 500, 'multi_select': 500, 'build_queue': 10}
        nonspatial_stack = np.concatenate(((self.max_units_seen,self.used_actions['N'],self.used_actions[self.race], available_actions, [self.last_action_used])))
        for feature_label, feature in observation.observation.items():
       	        if feature_label not in spatial_features + variable_features + ['available_actions']:
                        nonspatial_stack = np.concatenate((nonspatial_stack, feature.reshape(-1)))
       	        elif feature_label in variable_features:
                        padded_feature = np.concatenate((feature.reshape(-1), np.zeros(max_no[feature_label] * _SELECT_SIZE - len(feature.reshape(-1)))))
                        nonspatial_stack = np.concatenate((nonspatial_stack, padded_feature))

        nonspatial_stack = np.expand_dims(nonspatial_stack, axis=0)
        # spatial_minimap features
        minimap_stack = np.expand_dims(np.stack(features['minimap'], axis=2), axis=0)
        # spatial_screen features
        screen_stack = np.expand_dims(np.stack(features['screen'], axis=2), axis=0)
        # is episode over?
        episode_end = observation.step_type == environment.StepType.LAST
        return reward, nonspatial_stack, minimap_stack, screen_stack, episode_end

    def get_action(self, action_index):
        if action_index < len(self.general_actions):
            return actions.FUNCTIONS[self.general_actions[action_index]]
        else:
            action_index -= len(self.general_actions)
            return actions.FUNCTIONS[self.race_actions[action_index]]
    
    def act(self, action_selected, action_arguments):
        self.last_action_used = action_selected
        #Determine whether action was race specific or not
        if action_selected < len(self.general_actions):
            action_type = 'N'
        else:
            action_type = self.race
            action_selected -= len(self.general_actions)
        #Update how many times action was called
        self.used_actions[action_type][action_selected] += 1

        
    def calculate_nonspatial_size(self):
        #Add action space sizes for tracking which actions we can take
        size = self.action_count * 2 #Multiply by two for entries that keep track of how many times used
        #Increase size by number of unit types for enemies seen
        size += self.max_units_seen.size + 1 # +1 for last action used
        #Increase size by nonspatial structured observation data:
        nonspatial_features = features.Features(screen_size_px=(self.screen_size,self.screen_size), minimap_size_px=(self.minimap_size,self.minimap_size)).observation_spec()
        del nonspatial_features['minimap']
        del nonspatial_features['screen']
        for feature_label, feature in nonspatial_features.items():
                if feature_label in self.variable_features:
                        size += self.variable_features[feature_label] * feature[1]
                else:
                        size += np.prod(feature)
        return size




## ACTOR-CRITIC NETWORK

class AC_Network():
	def __init__(self, scope, trainer, agent_model = None):#action_spec, observation_spec):
		with tf.variable_scope(scope):
                        self.model = agent_model
			# Architecture here follows Atari-net Agent described in [1] Section 4.3
                        self.inputs_nonspatial = tf.placeholder(shape=[None,self.model.nonspatial_size], dtype=tf.float32)
                        self.inputs_spatial_screen = tf.placeholder(shape=[None,self.model.screen_size,self.model.screen_size,self.model.screen_channels], dtype=tf.float32)
                        self.inputs_spatial_minimap = tf.placeholder(shape=[None,self.model.minimap_size,self.model.minimap_size,self.model.minimap_channels], dtype=tf.float32)
                        self.nonspatial_dense = tf.layers.dense(
                                inputs=self.inputs_nonspatial,
                                units=32,
                                activation=tf.tanh)
                        self.screen_conv1 = tf.layers.conv2d(
                                inputs=self.inputs_spatial_screen,
                                filters=16,
                                kernel_size=[8,8],
                                strides=[4,4],
                                padding='valid',
                                activation=tf.nn.relu)
                        self.screen_conv2 = tf.layers.conv2d(
                                inputs=self.screen_conv1,
                                filters=32,
                                kernel_size=[4,4],
                                strides=[2,2],
                                padding='valid',
                                activation=tf.nn.relu)
                        self.minimap_conv1 = tf.layers.conv2d(
                                inputs=self.inputs_spatial_minimap,
                                filters=16,
                                kernel_size=[8,8],
                                strides=[4,4],
                                padding='valid',
                                activation=tf.nn.relu)
                        self.minimap_conv2 = tf.layers.conv2d(
                                inputs=self.minimap_conv1,
                                filters=32,
                                kernel_size=[4,4],
                                strides=[2,2],
                                padding='valid',
                                activation=tf.nn.relu)

			# According to [1]: "The results are concatenated and sent through a linear layer with a ReLU activation."
                        screen_output_length = 1
                        for dim in self.screen_conv2.get_shape().as_list()[1:]:
                                screen_output_length *= dim
                        minimap_output_length = 1
                        for dim in self.minimap_conv2.get_shape().as_list()[1:]:
                                minimap_output_length *= dim

                        self.latent_vector = tf.layers.dense(
                                inputs=tf.concat([self.nonspatial_dense, tf.reshape(self.screen_conv2,shape=[-1,screen_output_length]), tf.reshape(self.minimap_conv2,shape=[-1,minimap_output_length])], axis=1),
                                units=256,
                                activation=tf.nn.relu)

			# Output layers for policy and value estimations
			# 1 policy network for base actions
			# 16 policy networks for arguments
			#   - All modeled independently
			#   - Spatial arguments have the x and y values modeled independently as well
			# 1 value network
                        self.policy_base_actions = tf.layers.dense(
                                inputs=self.latent_vector,
                                units=self.model.action_count,
                                activation=tf.nn.softmax,
                                kernel_initializer=normalized_columns_initializer(0.01))
                        self.policy_arg = dict()
                        for arg in actions.TYPES:
                                self.policy_arg[arg.name] = dict()
                                for dim, size in enumerate(arg.sizes):
                                        processed_size = size
                                        if size == 0:
                                                processed_size = 1
                                                if arg.name == 'screen' or arg.name == 'screen2':
                                                        processed_size = self.model.screen_size
                                                elif arg.name == 'minimap':
                                                        processed_size = self.model.minimap_size
                                        self.policy_arg[arg.name][dim] = tf.layers.dense(
                                                inputs=self.latent_vector,
                                                units=processed_size,
                                                activation=tf.nn.softmax,
                                                kernel_initializer=normalized_columns_initializer(0.01))
                        self.value = tf.layers.dense(
                                inputs=self.latent_vector,
                                units=1,
                                kernel_initializer=normalized_columns_initializer(1.0))

			# Only the worker network need ops for loss functions and gradient updating.
                        if scope != 'global':
                                self.actions_base = tf.placeholder(shape=[None],dtype=tf.int32)
                                self.actions_onehot_base = tf.one_hot(self.actions_base,self.model.action_count,dtype=tf.float32)
                                self.actions_arg = dict()
                                self.actions_onehot_arg = dict()
                                for arg in actions.TYPES:
                                        self.actions_arg[arg.name] = dict()
                                        self.actions_onehot_arg[arg.name] = dict()
                                        for dim, size in enumerate(arg.sizes):
                                                processed_size = size
                                                if size == 0:
                                                        processed_size = 1
                                                        if arg.name == 'screen' or arg.name == 'screen2':
                                                                processed_size = self.model.screen_size
                                                        elif arg.name == 'minimap':
                                                                processed_size = self.model.minimap_size
                                                self.actions_arg[arg.name][dim] = tf.placeholder(shape=[None],dtype=tf.int32)
                                                self.actions_onehot_arg[arg.name][dim] = tf.one_hot(self.actions_arg[arg.name][dim],processed_size,dtype=tf.float32)

                                self.target_v = tf.placeholder(shape=[None],dtype=tf.float32)
                                self.advantages = tf.placeholder(shape=[None],dtype=tf.float32)
                                self.responsible_outputs_base = tf.reduce_sum(self.policy_base_actions * self.actions_onehot_base, [1])
                                self.responsible_outputs_arg = dict()
                                for arg in actions.TYPES:
                                        self.responsible_outputs_arg[arg.name] = dict()
                                        for dim, size in enumerate(arg.sizes):
                                                self.responsible_outputs_arg[arg.name][dim] = tf.reduce_sum(self.policy_arg[arg.name][dim] * self.actions_onehot_arg[arg.name][dim], [1])

				# Loss functions
                                self.value_loss = 0.5 * tf.reduce_sum(tf.square(self.target_v - tf.reshape(self.value,[-1])))
                                self.entropy_base = - tf.reduce_sum(self.policy_base_actions * tf.log(tf.clip_by_value(self.policy_base_actions, 1e-20, 1.0))) # avoid NaN with clipping when value in policy becomes zero
                                self.entropy_arg = dict()
                                for arg in actions.TYPES:
                                        self.entropy_arg[arg.name] = dict()
                                        for dim, size in enumerate(arg.sizes):
                                                self.entropy_arg[arg.name][dim] = - tf.reduce_sum(self.policy_arg[arg.name][dim] * tf.log(tf.clip_by_value(self.policy_arg[arg.name][dim], 1e-20, 1.)))
                                self.entropy = self.entropy_base
                                for arg in actions.TYPES:
                                        for dim, size in enumerate(arg.sizes):
                                                self.entropy += self.entropy_arg[arg.name][dim]
                                #
                                self.policy_loss_base = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_base, 1e-20, 1.0))*self.advantages)
                                self.policy_loss_arg = dict()
                                for arg in actions.TYPES:
                                        self.policy_loss_arg[arg.name] = dict()
                                        for dim, size in enumerate(arg.sizes):
                                                self.policy_loss_arg[arg.name][dim] = - tf.reduce_sum(tf.log(tf.clip_by_value(self.responsible_outputs_arg[arg.name][dim], 1e-20, 1.0)) * self.advantages)
                                #
                                self.policy_loss = self.policy_loss_base
                                for arg in actions.TYPES:
                                        for dim, size in enumerate(arg.sizes):
                                                self.policy_loss += self.policy_loss_arg[arg.name][dim]
                                self.loss = 0.5 * self.value_loss + self.policy_loss - self.entropy * 0.01

				# Get gradients from local network using local losses
                                local_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope)
				# self.gradients - gradients of loss wrt local_vars
                                self.gradients = tf.gradients(self.loss,local_vars)
                                self.var_norms = tf.global_norm(local_vars)
                                grads,self.grad_norms = tf.clip_by_global_norm(self.gradients,40.0)

				# Apply local gradients to global network
                                global_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, 'global')
                                self.apply_grads = trainer.apply_gradients(zip(grads,global_vars))

## WORKER AGENT

class Worker():
        def __init__(self,name,trainer,model_path,global_episodes, map_name, agent_model):
                self.name = "worker_" + str(name)
                self.number = name
                self.model_path = model_path
                self.trainer = trainer
                self.global_episodes = global_episodes
                self.increment = self.global_episodes.assign_add(1)
                self.episode_rewards = []
                self.episode_lengths = []
                self.episode_mean_values = []
                self.summary_writer = tf.summary.FileWriter("train_"+str(self.number))
                #Create the local copy of the network and the tensorflow op to copy global paramters to local network
                self.local_AC = AC_Network(self.name,trainer,agent_model)
                self.update_local_ops = update_target_graph('global',self.name)
                print('Initializing environment #{}...'.format(self.number))
                self.env = sc2_env.SC2Env(map_name=map_name,screen_size_px=(agent_model.screen_size,agent_model.screen_size), minimap_size_px=(agent_model.minimap_size,agent_model.minimap_size))
        def train(self,rollout,sess,gamma,bootstrap_value):
                rollout = np.array(rollout)
                obs_screen = rollout[:,0]
                obs_minimap = rollout[:,1]
                obs_nonspatial = rollout[:,2]
                actions_base = rollout[:,3]
                actions_args = rollout[:,4]
                rewards = rollout[:,5]
                #next_obs_screen = rollout[:,6]
                #next_obs_minimap = rollout[:,7]
                #next_obs_nonspatial = rollout[:,8]
                values = rollout[:,10]
                actions_arg_stack = dict()
                for actions_arg in actions_args:
                        for arg_name,arg in actions_arg.items():
                                if arg_name not in actions_arg_stack:
                                        actions_arg_stack[arg_name] = dict()
                                for dim, value in arg.items():
                                        if dim not in actions_arg_stack[arg_name]:
                                                actions_arg_stack[arg_name][dim] = []
                                        actions_arg_stack[arg_name][dim].append(value)
		# Here we take the rewards and values from the rollout, and use them to calculate the advantage and discounted returns
		# The advantage function uses generalized advantage estimation from [2]
                self.rewards_plus = np.asarray(rewards.tolist() + [bootstrap_value])
                discounted_rewards = discount(self.rewards_plus,gamma)[:-1]
                self.value_plus = np.asarray(values.tolist() + [bootstrap_value])
                advantages = rewards + gamma * self.value_plus[1:] - self.value_plus[:-1]
                advantages = discount(advantages,gamma)
                # Update the global network using gradients from loss
		# Generate network statistics to periodically save
                feed_dict = {self.local_AC.target_v:discounted_rewards,
                             self.local_AC.inputs_spatial_screen:np.stack(obs_screen).reshape(-1,self.local_AC.model.screen_size,self.local_AC.model.screen_size,self.local_AC.model.screen_channels),
                             self.local_AC.inputs_spatial_minimap:np.stack(obs_minimap).reshape(-1,self.local_AC.model.minimap_size,self.local_AC.model.minimap_size,self.local_AC.model.minimap_channels),
                             self.local_AC.inputs_nonspatial:np.stack(obs_nonspatial).reshape(-1,self.local_AC.model.nonspatial_size),
                             self.local_AC.actions_base:actions_base,
                             self.local_AC.advantages:advantages}
                for arg_name, arg in actions_arg_stack.items():
                        for dim, value in arg.items():
                                feed_dict[self.local_AC.actions_arg[arg_name][dim]] = value
		
                v_l,p_l,e_l,g_n,v_n, _ = sess.run([self.local_AC.value_loss,
                                                   self.local_AC.policy_loss,
                                                   self.local_AC.entropy,
                                                   self.local_AC.grad_norms,
                                                   self.local_AC.var_norms,
                                                   self.local_AC.apply_grads],
                                                  feed_dict=feed_dict)
                return v_l / len(rollout),p_l / len(rollout),e_l / len(rollout), g_n,v_n
		
        def work(self,max_episode_length,gamma,sess,coord,saver):
                episode_count = sess.run(self.global_episodes)
                total_steps = 0
                print ("Starting worker " + str(self.number))
                with sess.as_default(), sess.graph.as_default():				 
                        while not coord.should_stop():
                                #Download copy of parameters from global network
                                sess.run(self.update_local_ops)

                                episode_buffer = []
                                episode_values = []
                                episode_frames = []
                                episode_reward = 0
                                episode_step_count = 0
                                episode_end = False
                                #Start new episode
                                obs = self.env.reset()
                                self.local_AC.model.reset()
                                episode_frames.append(obs[0])
                                reward, nonspatial_stack, minimap_stack, screen_stack, episode_end = self.local_AC.model.process_observation(obs[0])
                                s_screen = screen_stack
                                s_minimap = minimap_stack
                                s_nonspatial = nonspatial_stack
                                while not episode_end:
                                        # Take an action using distributions from policy networks' outputs
                                        base_action_dist, arg_dist, v = sess.run([self.local_AC.policy_base_actions, self.local_AC.policy_arg, self.local_AC.value],
                                                                                 feed_dict={self.local_AC.inputs_spatial_screen: screen_stack,
                                                                                            self.local_AC.inputs_spatial_minimap: minimap_stack,
                                                                                            self.local_AC.inputs_nonspatial: nonspatial_stack})
                                        #Apply filter to remove unavailable actions and then renormalize
                                        for action_id, action_prob in enumerate(base_action_dist[0]):
                                                if self.local_AC.model.get_action(action_id).id not in obs[0].observation['available_actions']:
                                                        base_action_dist[0][action_id] = 0
                                        if np.sum(base_action_dist[0]) != 1 and np.sum(base_action_dist[0]) != 0:
                                                current_sum = np.sum(base_action_dist[0])
                                                base_action_dist[0] /= current_sum
                                        base_action = sample_dist(base_action_dist)
                                        arg_sample = dict()
                                        for arg in arg_dist:
                                                arg_sample[arg] = dict()
                                                for dim in arg_dist[arg]:
                                                        arg_sample[arg][dim] = sample_dist(arg_dist[arg][dim])

                                        arguments = []
                                        chosen_action = self.local_AC.model.get_action(base_action)
                                        for arg in chosen_action.args:
                                                arg_value = []
                                                for dim, size in enumerate(arg.sizes):
                                                        arg_value.append(arg_sample[arg.name][dim])
                                                arguments.append(arg_value)

					# Set unused arguments to -1 so that they won't be updated in the training
					# See documentation for tf.one_hot
                                        for arg_name, arg in arg_sample.items():
                                                if arg_name not in chosen_action.args:
                                                        for dim in arg:
                                                                arg_sample[arg_name][dim] = -1
                                        a = actions.FunctionCall(chosen_action.id, arguments)
                                        obs = self.env.step(actions=[a])
                                        self.local_AC.model.act(base_action,arguments)
                                        
                                        r, nonspatial_stack, minimap_stack, screen_stack, episode_end = self.local_AC.model.process_observation(obs[0])
                                        if not episode_end:
                                                episode_frames.append(obs[0])
                                                s1_screen = screen_stack
                                                s1_minimap = minimap_stack
                                                s1_nonspatial = nonspatial_stack
                                        else:
                                                s1_screen = s_screen
                                                s1_minimap = s_minimap
                                                s1_nonspatial = s_nonspatial
                                        #Append latest state to buffer
                                        episode_buffer.append([s_screen, s_minimap, s_nonspatial,base_action,arg_sample,r,s1_screen, s1_minimap, s1_nonspatial,episode_end,v[0,0]])
                                        episode_values.append(v[0,0])
                                        episode_reward += r
                                        s_screen = s1_screen
                                        s_minimap = s1_minimap
                                        s_nonspatial = s1_nonspatial
                                        total_steps += 1
                                        episode_step_count += 1
                                        #If the episode hasn't ended, but the experience buffer is full, then we make an update step using that experience rollout
                                        if len(episode_buffer) == self.local_AC.model.max_episodes_kept and not episode_end and episode_step_count != max_episode_length - 1:
                                                #Since we don't know what the true final return is, we "bootstrap" from our current value estimation
                                                v1 = sess.run(self.local_AC.value, 
                                                              feed_dict={self.local_AC.inputs_spatial_screen: screen_stack,self.local_AC.inputs_spatial_minimap: minimap_stack,self.local_AC.inputs_nonspatial: nonspatial_stack})[0,0]
                                                v_l,p_l,e_l,g_n,v_n = self.train(episode_buffer,sess,gamma,v1)
                                                episode_buffer = episode_buffer[len(episode_buffer)//2:]
                                                sess.run(self.update_local_ops)
                                        if episode_end:
                                                break

                                self.episode_rewards.append(episode_reward)
                                self.episode_lengths.append(episode_step_count)
                                self.episode_mean_values.append(np.mean(episode_values))
                                episode_count += 1
                                global _max_score, _running_avg_score, _episodes, _steps
                                if _max_score < episode_reward:
                                        _max_score = episode_reward
                                _running_avg_score += (episode_reward - _running_avg_score)/(episode_count if episode_count > 0 else 1)
                                _episodes[self.number] = episode_count
                                _steps[self.number] = total_steps

                                print("{} Step #{} Episode #{} Reward: {}".format(self.name, total_steps, episode_count, episode_reward))
                                print("Total Steps: {}\tTotal Episodes: {}\tMax Score: {}\tAvg Score: {}".format(np.sum(_steps), np.sum(_episodes), _max_score, _running_avg_score))
                                #Update the network using the episode buffer at the end of the episode
                                if len(episode_buffer) != 0:
                                        v_l,p_l,e_l,g_n,v_n = self.train(episode_buffer,sess,gamma,0.0)

                                if episode_count % self.local_AC.model.max_episodes_kept == 0 and episode_count != 0:
                                        if episode_count % self.local_AC.model.save_increment == 0 and self.name == 'worker_0':
                                                saver.save(sess,self.model_path+'/model-'+str(episode_count)+'.cptk')
                                                print ("Saved Model")
                                        mean_reward = np.mean(self.episode_rewards[-self.local_AC.model.max_episodes_kept:])
                                        mean_length = np.mean(self.episode_lengths[-self.local_AC.model.max_episodes_kept:])
                                        mean_value = np.mean(self.episode_mean_values[-self.local_AC.model.max_episodes_kept:])
                                        summary = tf.Summary()
                                        summary.value.add(tag='Perf/Reward', simple_value=float(mean_reward))
                                        summary.value.add(tag='Perf/Length', simple_value=float(mean_length))
                                        summary.value.add(tag='Perf/Value', simple_value=float(mean_value))
                                        summary.value.add(tag='Losses/Value Loss', simple_value=float(v_l))
                                        summary.value.add(tag='Losses/Policy Loss', simple_value=float(p_l))
                                        summary.value.add(tag='Losses/Entropy', simple_value=float(e_l))
                                        summary.value.add(tag='Losses/Grad Norm', simple_value=float(g_n))
                                        summary.value.add(tag='Losses/Var Norm', simple_value=float(v_n))
                                        self.summary_writer.add_summary(summary, episode_count)
                                        self.summary_writer.flush()
                                if self.name == 'worker_0':
                                        sess.run(self.increment)
def main():
        max_episode_length = 300
        gamma = .99 # Discount rate for advantage estimation and reward discounting
        load_model = False
        race = 'T'
        model_path = './model'+race
        map_name = FLAGS.map_name
        max_episodes_kept = 5
        agent_model = AgentModel(race=race, max_episodes_kept = max_episodes_kept)
        #assert map_name in mini_games.mini_games
        tf.reset_default_graph()
        if not os.path.exists(model_path):
                os.makedirs(model_path)
        with tf.device("/cpu:0"): 
                global_episodes = tf.Variable(0,dtype=tf.int32,name='global_episodes',trainable=False)
                trainer = tf.train.AdamOptimizer(learning_rate=1e-4)
                master_network = AC_Network('global',None, AgentModel(agent_model = agent_model)) # Generate global network
                #num_workers = multiprocessing.cpu_count() # Set workers to number of available CPU threads
                num_workers =1# psutil.cpu_count() # Set workers to number of available CPU threads
                global _max_score, _running_avg_score, _steps, _episodes
                _max_score = 0
                _running_avg_score = 0
                _steps = np.zeros(num_workers)
                _episodes = np.zeros(num_workers)
                workers = []
		# Create worker classes
                for i in range(num_workers):
                        workers.append(Worker(i,trainer,model_path,global_episodes, map_name, AgentModel(agent_model=agent_model)))
                saver = tf.train.Saver(max_to_keep=max_episodes_kept)

        with tf.Session() as sess:
                coord = tf.train.Coordinator()
                if load_model == True:
                        print ('Loading Model...')
                        ckpt = tf.train.get_checkpoint_state(model_path)
                        saver.restore(sess,ckpt.model_checkpoint_path)
                else:
                        print('Initializing all variables...')
                        sess.run(tf.global_variables_initializer())
                #This is where the asynchronous magic happens
		# Start the "work" process for each worker in a separate thread
                worker_threads = []
                for worker in workers:
                        worker_work = lambda: worker.work(max_episode_length,gamma,sess,coord,saver)
                        t = threading.Thread(target=(worker_work))
                        t.start()
                        sleep(0.125)
                        worker_threads.append(t)
                coord.join(worker_threads)

if __name__ == '__main__':
        flags.DEFINE_string("map_name", "DefeatRoaches", "Name of the map/minigame")
        FLAGS(sys.argv)
        main()
