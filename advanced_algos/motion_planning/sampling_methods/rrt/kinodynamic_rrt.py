import numpy as np
from tqdm import tqdm

class KinodynamicRRT:
    def __init__(self, start, goal, obstacle_free, max_iters, state_limits, u_set, dynamics_model, control_duration, dt, goal_threshold):
        """
        Initialize the Kinodynamic RRT.
        """
        self.start = np.array(start)
        self.goal = np.array(goal)
        self.obstacle_free = obstacle_free
        self.max_iters = max_iters
        self.state_limits = state_limits
        self.u_set = u_set
        self.dynamics_model = dynamics_model
        self.control_duration = control_duration
        self.dt = dt
        self.goal_threshold = goal_threshold
        self.tree = [self.start]
        self.parent = {tuple(self.start): None}
        self.controls = {tuple(self.start): None}
        self.all_edges = []
        self.num_nodes = 1

    def sample_state(self, goal_bias=0.1):
        """Randomly sample a state within the state limits or with a goal bias."""
        if np.random.rand() < goal_bias:
            return self.goal
        else:
            return np.array([np.random.uniform(*limit) for limit in self.state_limits])
        
    def nearest(self, tree, state, weights=None):
        """Find the nearest state in the tree to the given state."""
        weights = np.ones(len(state)) if weights is None else weights
        distances = [
            np.linalg.norm((np.array(node) - state) * weights) for node in tree
        ]
        nearest_idx = np.argmin(distances)
        return tree[nearest_idx]

    def propagate(self, state, control, method='euler'):
        """
        Propagate the state using the given control over the fixed control duration.
        """
        num_steps = int(self.control_duration / self.dt)
        new_state = state
        for _ in range(num_steps):
            new_state = self.dynamics_model.step(new_state, control, self.dt, method)
        return new_state

    def plan(self, integration_method='euler'):
        """
        Execute the Kinodynamic RRT planning algorithm.
        """
        for _ in tqdm(range(self.max_iters)):
            x_rand = self.sample_state()
            x_nearest = self.nearest(self.tree, x_rand)
            
            closest_new_node = None
            closest_distance = float('inf')
            closest_control = None
            
            # Try all controls and find the closest new node to x_rand
            for control in self.u_set:
                x_new = self.propagate(x_nearest, control, method=integration_method)
                
                if self.obstacle_free(x_nearest, x_new) and not any(np.allclose(x_new, node) for node in self.tree):
                    distance = np.linalg.norm(x_new[:len(self.goal)] - x_rand)
                    
                    if distance < closest_distance:
                        closest_new_node = x_new
                        closest_distance = distance
                        closest_control = control

            # Add the closest new node to the tree if found
            if closest_new_node is not None:
                self.tree.append(closest_new_node)
                self.parent[tuple(closest_new_node)] = tuple(x_nearest)
                self.controls[tuple(closest_new_node)] = closest_control
                self.all_edges.append((x_nearest, closest_new_node))
                self.num_nodes += 1

                # Check if this new node is within the goal threshold
                if np.linalg.norm(closest_new_node[:len(self.goal)] - self.goal) <= self.goal_threshold:
                    return self.reconstruct_path(closest_new_node)
                
        return None

    def reconstruct_path(self, end_state):
        """Reconstruct the path from start to the given end_state."""
        path = []
        state = tuple(end_state)
        while state is not None:
            path.append(np.array(state))
            state = self.parent.get(state)
        path.reverse()
        return path
