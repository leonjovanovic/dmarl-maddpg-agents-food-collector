import numpy as np
import torch

import Config
from NN import PolicyNN, CriticNN

torch.set_printoptions(threshold=10_000)
class AgentControl:
    def __init__(self, state_shape, action_cont_shape, action_disc_shape, num_agents):
        self.action_cont_shape = action_cont_shape
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.moving_policy_nn = []
        self.moving_critic_nn = []
        self.policy_nn_optim = []
        self.critic_nn_optim = []
        self.target_policy_nn = []
        self.target_critic_nn = []
        for i in range(num_agents):
            self.moving_policy_nn.append(
                PolicyNN(state_shape[0] * state_shape[1] * state_shape[2], action_cont_shape, action_disc_shape).to(
                    self.device))
            self.policy_nn_optim.append(
                torch.optim.Adam(self.moving_policy_nn[i].parameters(), lr=Config.policy_lr, eps=Config.adam_eps))
            self.moving_critic_nn.append(CriticNN(
                num_agents * state_shape[0] * state_shape[1] * state_shape[2] + num_agents * (
                            action_cont_shape + 1)).to(self.device))
            self.critic_nn_optim.append(
                torch.optim.Adam(self.moving_critic_nn[i].parameters(), lr=Config.critic_lr, eps=Config.adam_eps))
            self.target_policy_nn.append(
                PolicyNN(state_shape[0] * state_shape[1] * state_shape[2], action_cont_shape, action_disc_shape).to(
                    self.device))
            self.target_critic_nn.append(CriticNN(
                num_agents * state_shape[0] * state_shape[1] * state_shape[2] + num_agents * (
                            action_cont_shape + 1)).to(self.device))
        self.mse = torch.nn.MSELoss()
        self.noise_std = 0.1

    def get_actions(self, state, n_step, buffer):
        # Transform 20x40x40x5 to 20x8000
        state_t = torch.Tensor(state).to(self.device)#torch.flatten(torch.Tensor(state).to(self.device), start_dim=1)
        # NN output will be 1x3 and 1x2, we need to stack them to 20x3 and 20x2
        action_cont = torch.zeros((state.shape[0], 3)).to(self.device)
        action_disc = torch.zeros((state.shape[0], 1)).to(self.device)
        # actions = torch.zeros((state.shape[0], 4)).to(self.device)
        for i in range(Config.num_of_agents):
            action_cont[i * Config.num_of_envs: (i + 1) * Config.num_of_envs, :], action_disc[i * Config.num_of_envs: (i + 1) * Config.num_of_envs, :] = \
                self.moving_policy_nn[i](state_t[i * Config.num_of_envs: (i + 1) * Config.num_of_envs])
        #for i in range(Config.num_of_envs * Config.num_of_agents):
        #    action_cont[i, :], action_disc[i, :] = self.moving_policy_nn[i % Config.num_of_agents](state_t[i, :])
        if buffer.buffer_index >= Config.min_buffer_size or buffer.initialized:
            print(f'Step {n_step}: Agent#0{action_cont[0].tolist()} Agent#1{action_cont[1].tolist()}')
        noise = (self.noise_std ** 0.5) * torch.randn((state.shape[0], 3)).to(self.device)
        action_cont = torch.clip(action_cont + noise, -1, 1).detach().cpu().numpy()
        # Razlika izmedju generisanog broja od 0 do 1 i verovatnoce
        # choices = np.random.random((state.shape[0], 1)) - action_disc_prob.detach().cpu().numpy()[:, :1]
        # Veci jednak od 0 => 1, manji od nule => 0
        # action_disc = np.array(np.greater(choices, 0), dtype=int)
        return action_cont, action_disc.detach().cpu().numpy()

    def get_actions_random(self, state):
        action_cont = np.random.random((state.shape[0], self.action_cont_shape)) * 2 - 1
        action_disc = np.round(np.random.random((state.shape[0], 1)))
        #actions = np.random.random((state.shape[0], self.action_cont_shape + 1)) * 2 - 1
        return action_cont, action_disc

    def lr_std_decay(self, n_step):
        frac = 1 - n_step / Config.total_steps
        for i in range(Config.num_of_agents):
            self.policy_nn_optim[i].param_groups[0]["lr"] = frac * Config.policy_lr
            self.critic_nn_optim[i].param_groups[0]["lr"] = frac * Config.critic_lr
        #self.noise_std = self.noise_std * frac

    def critic_update(self, states, actions, rewards, new_states, dones):
        # States shape = batch_size x num_of_agents x 8000 (e.g.64x5x8000), action shape = batch_size x num_of_agents x 4
        # CriticNN input shape = num_of_agents x 8000 + num_of_agents x 4
        # state_f & new_state_f shape = 64 x 40000, action_f & new_action_f shape = 64 x 20
        states.to('cuda')
        actions.to('cuda')
        rewards = rewards.to('cuda') * 10
        new_states = new_states.to('cuda')
        dones = dones.to('cuda')
        states_f = torch.flatten(states.to('cuda'), start_dim=1)
        actions_f = torch.flatten(actions.to('cuda'), start_dim=1)
        new_states_f = torch.flatten(new_states, start_dim=1)
        new_actions_f = torch.zeros(actions_f.shape).to(self.device)
        # Finding new actions for each agent. We need all actions for each CriticNN THIS WILL CHANGE IN THE FUTURE
        for i in range(Config.num_of_agents):
            action_cont, action_disc = self.target_policy_nn[i](new_states[:, i, :])
            #choices = np.random.random((action_disc_prob.shape[0], 1)) - action_disc_prob.detach().cpu().numpy()[:, :1]
            #action_disc = np.array(np.greater(choices, 0), dtype=int)
            new_action = torch.cat((action_cont, action_disc), dim=1)
            new_actions_f[:, i * new_action.shape[1]: (i + 1) * new_action.shape[1]] = new_action.detach()
        # Input for Target CriticNN = new state and new action (DETACHED), input for Moving CriticNN = state and action
        critic_losses = []
        for i in range(Config.num_of_agents):
            # NN output shape = batch_size x 1
            state_values = self.moving_critic_nn[i](states_f, actions_f).squeeze(-1)
            new_state_values = self.target_critic_nn[i](new_states_f, new_actions_f).squeeze(-1).detach()
            # rewards shape = batch_size x 1, CriticNN output shape = batch_size x 1
            target = rewards[:, i] + Config.gamma * new_state_values * (1 - dones[:, i])
            critic_losses.append(self.mse(target, state_values))

            self.critic_nn_optim[i].zero_grad()
            critic_losses[i].backward()
            torch.nn.utils.clip_grad_norm_(self.moving_critic_nn[i].parameters(), max_norm=0.5)
            self.critic_nn_optim[i].step()
            critic_losses[i] = critic_losses[i].detach().cpu()
        return critic_losses

    def policy_update(self, states, actions):
        states = states.to('cuda')
        actions = actions.to('cuda')
        states_f = torch.flatten(states, start_dim=1)
        policy_losses = []
        for i in range(Config.num_of_agents):
            action_cont, action_disc = self.moving_policy_nn[i](states[:, i, :])
            actions_agent = actions.detach().clone()
            actions_agent[:, i, :] = torch.cat((action_cont, action_disc), dim=1)
            actions_f = torch.flatten(actions_agent, start_dim=1)
            policy_loss = self.moving_critic_nn[i](states_f, actions_f).squeeze(-1)
            policy_loss = -torch.mean(policy_loss)
            policy_losses.append(policy_loss)

            self.policy_nn_optim[i].zero_grad()
            policy_losses[i].backward()
            torch.nn.utils.clip_grad_norm_(self.moving_policy_nn[i].parameters(), max_norm=0.5)
            self.policy_nn_optim[i].step()
            policy_losses[i] = policy_losses[i].detach().cpu()
        return policy_losses

    def target_update(self):
        # Update target networks by polyak averaging.
        with torch.no_grad():
            for i in range(Config.num_of_agents):
                for mov, targ in zip(self.moving_critic_nn[i].parameters(), self.target_critic_nn[i].parameters()):
                    # NB: We use an in-place operations "mul_", "add_" to update target
                    # params, as opposed to "mul" and "add", which would make new tensors.
                    targ.data.mul_(Config.polyak)
                    targ.data.add_((1 - Config.polyak) * mov.data)

                for mov, targ in zip(self.moving_policy_nn[i].parameters(), self.target_policy_nn[i].parameters()):
                    targ.data.mul_(Config.polyak)
                    targ.data.add_((1 - Config.polyak) * mov.data)
