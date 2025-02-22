import uuid
import random
import numpy as np
import matplotlib.pyplot as plt

from scipy.spatial import KDTree
from advanced_algos.motion_planning.smoothing import FSBAS

class Node:
    def __init__(self, state, tree_type, parent=None, segment_time=None, trajectory_info=None):
        """
        Initialize a Node.

        Args:
            state (np.ndarray): The state of the node.
            tree_type (str): The type of the tree ('forward' or 'backward') to which this node belongs.
            parent (Node or None): The parent node in the tree.
            segment_time (float): The time required to traverse the segment leading to this node.
            trajectory_info (object): Trajectory-related information for this node.
        """
        self.state = state
        assert tree_type == 'forward' or tree_type == 'backward'
        self.tree_type = tree_type
        self.parent = parent
        self.segment_time = segment_time
        self.trajectory_info = trajectory_info
        self.children = []

        # Generate a unique identifier for this node
        self.uuid = uuid.uuid4()

class RampPlanner:
    def __init__(self, start, goal, max_iters, collision_checker, position_limits, vmax, amax):
        """
        Initialize the RampPlanner.

        Args:
            start (np.ndarray): Initial state as a concatenated position and velocity vector.
                                 Shape: (2 * n_dimensions,)
            goal (np.ndarray): Goal state as a concatenated position and velocity vector.
                                Shape: (2 * n_dimensions,)
            max_iters (int): Maximum number of iterations for the planning loop.
            collision_checker (callable): Function to check for collisions.
                                          It should accept a state (np.ndarray) and return False if there is a collision,
                                          True otherwise.
            position_limits (tuple of np.ndarray): Position limits as (min_pos, max_pos).
                                                   Each is a NumPy array of shape (n_dimensions,).
            vmax (np.ndarray): Maximum velocity magnitudes for each dimension.
                                Shape: (n_dimensions,)
            amax (np.ndarray): Maximum acceleration magnitudes for each dimension.
                                Shape: (n_dimensions,)
        """
        # Initialize start and goal using Node class. 
        self.start = Node(state=start, tree_type='forward')
        self.goal = Node(state=goal, tree_type='backward')

        # Initialize forward tree and backward tree.
        self.forward_tree = []
        self.backward_tree = []

        self.max_iters = max_iters
        self.collision_checker = collision_checker
        self.position_limits = position_limits
        self.vmax = np.array(vmax, dtype=float)
        self.amax = np.array(amax, dtype=float)
        self.dimension = self.vmax.shape[0]

        self.init_weights()

        self.path = None
        self.smoothed_path = None
        self.visualization = False

    def init_weights(self):
        # Compute weights based on position and velocity limits
        min_pos, max_pos = self.position_limits
        position_weights = 1.0 / (max_pos - min_pos)**2  # Position weights
        velocity_weights = 1.0 / (self.vmax * 2)**2      # Velocity weights (assuming symmetrical limits)

        # Combine position and velocity weights
        self.weights = np.concatenate([position_weights, velocity_weights])

    def plan(self, smooth_iterations=10, visualize=False, visualization_args=None):
        """
        Plan the trajectory from the start to the goal state.

        Args:
            smooth_iterations: Number of smooth iteration
            visualize (bool): Whether to visualize the planning process.
            visualization_args (dict or None): Additional arguments for visualization, such as obstacles.
            
        Returns:
            list of np.ndarray or None: Planned trajectory as a list of concatenated position and velocity vectors if successful, else None.
        """
        # Seed the trees with maximum braking trajectories
        if not self.seed_trees():
            print("Failed to seed trees. Exiting.")
            return None  # Fail if this step fails

        # Initialize visualization if needed
        if visualize:
            self.visualization = True
            self.visualization_args = visualization_args

            self.fig, self.ax = plt.subplots(figsize=(8, 8))
            self._initialize_plot()

        # Define forward and backward trees for alternating extension
        trees = [
            (self.forward_tree, self.backward_tree),
            (self.backward_tree, self.forward_tree),
        ]

        # Main planning loop
        for i in range(self.max_iters):
            # Select current tree and its counterpart
            current_tree, other_tree = trees[i % 2]

            # Extend the selected tree
            extended_node = self.extend_tree(current_tree)

            if extended_node is not None:
                # Check if the extended node can connect to the other tree
                paths = self.connect_trees(other_tree, extended_node)
                if paths is not None:
                    # Check for collisions and connect paths if collision-free
                    result = self._check_paths_collision(paths)
                    if isinstance(result, list):  # If the result is the connected path
                        # Apply path smoothing and return the final path
                        reconstructed_path = self.reconstruct_path(result)
                        self.path = reconstructed_path
                        if self.visualization:
                            self._update_plot()

                        self.smoothed_path = self.smooth_path(reconstructed_path, max_iterations=smooth_iterations)
                        if self.visualization:
                            self._update_plot()

                        # Return the smoothed path as a list of states
                        return [node.state for node in self.smoothed_path]
                    else:
                        # Handle the infeasible result (could be an edge or other issue)
                        self.remove_infeasible_edge(result)

        # Return None if no path is found within the maximum iterations
        return None
    
    def seed_trees(self):
        """
        Seed the forward and backward trees with maximum braking trajectories.
        """
        for tree, seed_node, tree_type in [
            (self.forward_tree, self.start, 'forward'),
            (self.backward_tree, self.goal, 'backward'),
        ]:
            # Generate maximum braking trajectory from the root state
            initial_state, segment_time, trajectory_info = self.generate_max_braking_trajectory(seed_node.state)
            if initial_state is not None:
                # Create a new node and append it to the tree
                new_node = Node(
                    state=initial_state,
                    tree_type=tree_type,
                    parent=None,  # Seem as the root of the tree
                )
                tree.append(new_node)

                seed_node.segment_time = segment_time
                seed_node.trajectory_info = trajectory_info

                if not self._check_edge_collision(new_node, seed_node, segment_time, trajectory_info):
                    return False
                
        return True

    def generate_max_braking_trajectory(self, state):
        """
        Generate the initial zero-speed state and the trajectory to the given state.

        Args:
            state (np.ndarray): Target state as a concatenated position and velocity vector.
                                Shape: (2 * n_dimensions,)

        Returns:
            tuple or None:
                - initial_state (np.ndarray): The zero-speed state.
                - trajectory_info (list of tuples): Trajectory information for each dimension:
                    - trajectory_type (str): Always "P-P+" for acceleration.
                    - acceleration (float): Absolute acceleration used for acceleration in the dimension.
        """
        pos_dim = self.dimension

        # Extract target position and velocity
        position = state[:pos_dim]
        velocity = state[pos_dim:]

        # Calculate acceleration time for each dimension
        acceleration_times = np.abs(velocity) / self.amax
        t_max = np.max(acceleration_times)  # Use the maximum acceleration time

        # Initialize arrays for initial state and trajectory info
        initial_position = np.zeros_like(position)
        trajectory_info = []

        # Calculate acceleration for each dimension independently
        for i in range(pos_dim):
            if t_max > 0:
                # Compute acceleration to achieve target velocity in t_max
                a = velocity[i] / t_max

                # Compute initial position using Δp = -0.5 * a * t^2
                delta_p = -0.5 * a * t_max**2
                initial_position[i] = position[i] + delta_p

                # Store trajectory information
                trajectory_info.append((np.abs(a), "P+P-"))
            else:
                # No motion in this dimension
                initial_position[i] = position[i]
                trajectory_info.append((0.0, "P+P-"))  # No acceleration needed

        # Combine initial position and zero velocity into a single state
        initial_state = np.concatenate((initial_position, np.zeros_like(velocity)))

        # Check position limits first
        if not self._check_state_limits(initial_state):
            return None, None, None

        # Then check for collisions
        if not self.collision_checker(initial_state):
            return None, None, None

        return initial_state, t_max, trajectory_info

    def extend_tree(self, tree):
        """
        Extend the given tree by sampling and connecting states.

        Args:
            tree (list of Node): The tree to extend, containing Node objects.

        Returns:
            Node or None: The newly added node if successful, else None.
        """

        # Sample a Node from the tree
        sampled_node = random.choice(tree)

        # Sample a random state from free space
        random_state = self._sample_free_space_state()

        # Check collision for the sampled random state
        if not self.collision_checker(random_state):
            return None  # Collision detected, skip this extension

        # Compute the segment time and trajectory
        segment_time, trajectory_info = self._compute_optimal_segment(
            sampled_node.state.reshape(2, self.dimension),
            random_state.reshape(2, self.dimension)
        )

        # Create a new Node
        new_node = Node(
            state=random_state,
            tree_type=sampled_node.tree_type,
            parent=sampled_node,
            segment_time=segment_time,
            trajectory_info=trajectory_info,
        )

        # Append the new node to the tree
        tree.append(new_node)

        # Add the new node to the parent's children list
        sampled_node.children.append(new_node)

        # Update the plot if visualization is enabled
        if self.visualization:
            self._update_plot()

        return new_node

    def connect_trees(self, tree, node, epsilon=0.1):
        """
        Attempt to connect a given node to a tree.

        Args:
            tree (list of Node): The tree to connect to.
            node (Node): The node from the other tree.

        Returns:
            list of list[Node] or None: Two paths (from root to the connecting node in each tree)
                                        if connection is found, else None.
        """
        nearest_node, distance = self._find_nearest_node(tree, node)

        if distance > epsilon:
            return None

        # Construct paths for both trees
        path1 = self._trace_path(nearest_node)
        path2 = self._trace_path(node)

        paths = [path1, path2]

        return paths

    def reconstruct_path(self, path):
        """
        Adjust the path order to form a single continuous trajectory from start to end,
        and recalculate trajectory information for all nodes.

        Args:
            path (list of Node): The input path, potentially disordered.

        Returns:
            list of Node: A reordered and smoothed path from start to end.
        """
        if not path:
            return []

        reconstructed_path = []

        # Iterate through the path to create a new continuous sequence of nodes
        for i, node in enumerate(path):
            # Create a new copy of the node
            new_node = Node(
                state=node.state.copy(),
                tree_type="forward",  # Unified tree type for the smoothed path
                parent=None,          # Parent will be updated dynamically
            )

            if i > 0:  # Recalculate trajectory with the previous node
                prev_node = reconstructed_path[-1]

                if node.tree_type == 'forward':  # Parent-child relationship intact
                    new_node.segment_time = node.segment_time
                    new_node.trajectory_info = node.trajectory_info
                else:  # Relationship broken, recalculate
                    segment_time, trajectory_info = self._compute_optimal_segment(
                        prev_node.state.reshape(2, self.dimension),
                        new_node.state.reshape(2, self.dimension)
                    )
                    new_node.segment_time = segment_time
                    new_node.trajectory_info = trajectory_info

                # Set parent relationship
                new_node.parent = prev_node
                prev_node.children.append(new_node)

            reconstructed_path.append(new_node)

        return reconstructed_path
    
    def smooth_path(self, path, max_iterations=10):
        path_state = np.array([node.state.reshape(2, self.dimension) for node in path])
        fsbas = FSBAS(path=path_state, vmax=self.vmax, amax=self.amax, collision_checker=self.collision_checker, max_iterations=max_iterations)
        smoothed_path = fsbas.smooth_path()

        path = self._construct_path(smoothed_path)
        
        return path

    def remove_infeasible_edge(self, infeasible_edge):
        """
        Remove the infeasible edge from the tree.

        Args:
            infeasible_edge (tuple): A tuple (start_node, end_node, bridge_start_node, bridge_end_node) 
                                    representing the edge to be removed.

        Returns:
            None
        """
        start_node, end_node, bridge_start_node, bridge_end_node = infeasible_edge  # Unpack the tuple

        # Case 1: If the edge is a bridge, do nothing
        if start_node.tree_type != end_node.tree_type:
            return

        # Case 2: The edge is internal to a single tree
        old_tree = self.forward_tree if start_node.tree_type == 'forward' else self.backward_tree
        new_tree = self.backward_tree if start_node.tree_type == 'forward' else self.forward_tree

        # Determine the bridge node belonging to the same tree as end_node
        if bridge_start_node.tree_type == end_node.tree_type:
            bridge_in_same_tree = bridge_start_node
            bridge_in_other_tree = bridge_end_node
        else:
            bridge_in_same_tree = bridge_end_node
            bridge_in_other_tree = bridge_start_node

        # Start reconfiguring from the bridge node in the same tree
        current_node = bridge_in_same_tree
        new_parent_node = bridge_in_other_tree  # The initial new parent node from the other tree

        while current_node is not start_node:
            # Record the current parent node
            old_parent = current_node.parent

            # Update current node's parent to the new parent node
            current_node.parent = new_parent_node

            # Update parent's children relationship
            old_parent.children.remove(current_node)
            new_parent_node.children.append(current_node)

            # Update trajectory and segment time
            segment_time, trajectory_info = self._compute_optimal_segment(
                new_parent_node.state.reshape(2, self.dimension), 
                current_node.state.reshape(2, self.dimension)
            )
            current_node.segment_time = segment_time
            current_node.trajectory_info = trajectory_info

            # Update all child nodes' tree type before moving to the old parent
            stack = [current_node]
            while stack:
                node = stack.pop()
                # Update the tree type for the current node
                node.tree_type = new_parent_node.tree_type

                # Move the node from the old tree to the new tree
                old_tree.remove(node)
                new_tree.append(node)

                # Add all children of the current node to the stack for processing
                stack.extend(node.children)

            # Update the new parent node for the next iteration
            new_parent_node = current_node
            
            # Move to the old parent node
            current_node = old_parent

        # Update the visualization
        if self.visualization:
            self._update_plot()

    def _check_state_limits(self, state):
        """
        Check if the position part of the state is within the allowed limits.

        Args:
            state (np.ndarray): State as a concatenated position and velocity vector.
                                Shape: (2 * n_dimensions,)

        Returns:
            bool: True if within limits, False otherwise.
        """
        pos_indices = slice(0, self.dimension)  # Indices for position
        pos_min, pos_max = self.position_limits
        return np.all((state[pos_indices] >= pos_min) & (state[pos_indices] <= pos_max))
    
    def _weighted_euclidean_distance(self, state_from, state_to):
        """
        Calculate the weighted Euclidean distance between two states.

        Args:
            state_from (np.ndarray): The starting state as a concatenated position and velocity vector.
                                    Shape: (2 * n_dimensions,)
            state_to (np.ndarray): The target state as a concatenated position and velocity vector.
                                Shape: (2 * n_dimensions,)

        Returns:
            float: The weighted Euclidean distance between the two states.
        """
        diff = state_from - state_to  # Difference between the two states
        return np.sqrt(np.sum(self.weights * diff**2))  # Weighted Euclidean distance
    
    def _find_nearby_nodes(self, tree, target_node_state, epsilon):
        """
        Find all nodes in the tree that are within a given weighted distance threshold using KDTree.

        Args:
            tree (list of Node): The tree containing Node objects.
            target_node_state (np.ndarray): The target node state to check against.
            epsilon (float): Distance threshold to consider nodes as "nearby".

        Returns:
            list of Node: List of Node objects within the distance threshold.
            list of int: Indices of these nodes in the tree.
        """
        # Extract states from the Node objects in the tree
        tree_states = np.array([node.state for node in tree])
        
        # Apply weights to the tree states and target node state
        weighted_tree_states = tree_states * np.sqrt(self.weights)
        weighted_target_node_state = target_node_state * np.sqrt(self.weights)

        # Build a KDTree with the weighted states
        kdtree = KDTree(weighted_tree_states)

        # Query for nearby nodes within the weighted distance threshold
        nearby_indices = kdtree.query_ball_point(weighted_target_node_state, r=epsilon)

        # Retrieve the Node objects from the tree
        nearby_nodes = [tree[i] for i in nearby_indices]

        return nearby_nodes
    
    def _find_nearest_node(self, tree, target_node):
        """
        Find the nearest node in the tree to the target node using weighted distance.

        Args:
            tree (list of Node): The tree containing Node objects.
            target_node (Node): The target node to check against.

        Returns:
            tuple:
                - nearest_node (Node): The nearest Node object in the tree.
                - distance (float): The weighted distance to the nearest node.
        """
        # Extract states from the Node objects in the tree
        tree_states = np.array([node.state for node in tree])
        target_node_state = target_node.state

        # Apply weights to the tree states and target node state
        weighted_tree_states = tree_states * np.sqrt(self.weights)
        weighted_target_node_state = target_node_state * np.sqrt(self.weights)

        # Build a KDTree for efficient nearest neighbor search
        kdtree = KDTree(weighted_tree_states)

        # Query the KDTree for the nearest neighbor
        distance, nearest_index = kdtree.query(weighted_target_node_state)

        # Retrieve the nearest Node object from the tree
        nearest_node = tree[nearest_index]

        return nearest_node, distance
    
    def _sample_free_space_state(self):
        """
        Randomly sample a state from the free space with zero velocity.

        Returns:
            np.ndarray: A randomly sampled state with position in free space and zero velocity.
        """
        pos_dim = self.dimension
        # Sample position uniformly within position limits
        min_pos, max_pos = self.position_limits
        random_position = np.random.uniform(min_pos, max_pos)
        
        # Set velocity to zero
        zero_velocity = np.zeros(pos_dim)
        
        # Concatenate position and velocity
        return np.concatenate([random_position, zero_velocity])
    
    def _trace_path(self, node):
        """
        Construct a path by tracing back from a given node to the root.

        Args:
            node (Node): The node to start tracing back from.

        Returns:
            list of Node: Path as a list of Node objects, ordered from root to the given node.
        """
        path = []
        current_node = node

        # Trace back to the root
        while current_node is not None:
            path.append(current_node)
            if current_node.parent is not None:
                current_node = current_node.parent
            else:
                current_node = None

        # Reverse the path to go from root to the given node
        path.reverse()

        return path
    
    def _connect_paths(self, paths):
        """
        Connect two paths into a single path from start_node to end_node.

        Args:
            paths (list of list of Node): Two paths, where one starts with start_node
                                            and the other starts with end_node.

        Returns:
            list of Node: Combined path from start_node to end_node.
        """
        path1, path2 = paths

        # Identify the forward and backward paths based on tree type
        if path1[0].tree_type == "forward" and path2[0].tree_type == "backward":
            forward_path = path1
            backward_path = path2[::-1]  # Reverse the backward path
        elif path2[0].tree_type == "forward" and path1[0].tree_type == "backward":
            forward_path = path2
            backward_path = path1[::-1]  # Reverse the backward path
        else:
            raise ValueError("Invalid paths: Unable to determine start and end nodes.")

        # Combine paths
        combined_path = forward_path + backward_path

        # Insert seed nodes
        if not np.array_equal(self.start.state, combined_path[0].state):
            combined_path.insert(0, self.start)
        if not np.array_equal(self.goal.state, combined_path[-1].state):
            combined_path.append(self.goal)

        return combined_path
    
    def _construct_path(self, smoothed_path):
        """
        Construct a path for the smoothed path in the form of this class.
        """
        constructed_path = []

        for i in range(smoothed_path.shape[0]):
            new_node = Node(
                state=smoothed_path[i].reshape(-1),
                tree_type="forward",  # Unified tree type for the smoothed path
                parent=None,          # Parent will be updated dynamically
            )

            if i > 0:  # Recalculate trajectory with the previous node
                prev_node = constructed_path[-1]

                segment_time, trajectory_info = self._compute_optimal_segment(
                    prev_node.state.reshape(2, self.dimension),
                    new_node.state.reshape(2, self.dimension)
                )
                new_node.segment_time = segment_time
                new_node.trajectory_info = trajectory_info

                # Set parent relationship
                new_node.parent = prev_node
                prev_node.children.append(new_node)

            constructed_path.append(new_node)

        return constructed_path
    
    def _check_paths_collision(self, paths):
        """
        Internal method to check collisions for two paths and the connecting bridge.

        Args:
            paths (list of list[Node]): Two paths [path1, path2] as lists of Node objects.

        Returns:
            tuple or list[Node]:
                - (collision_segment_start, collision_segment_end): If a collision occurs.
                - list of Node: Full combined path if no collision occurs.
        """
        path1, path2 = paths

        bridge_start, bridge_end = path1[-1], path2[-1]  # Extract bridge nodes

        # Check collision for path1
        result = self._check_path_collision(path1)
        if result is not None:
            return (*result, bridge_start, bridge_end)  # Add bridge nodes for context

        # Check collision for path2
        result = self._check_path_collision(path2)
        if result is not None:
            return (*result, bridge_start, bridge_end)  # Add bridge nodes for context

        # Check collision for the bridge
        segment_time, trajectory_info = self._compute_optimal_segment(
            bridge_start.state.reshape(2, self.dimension),
            bridge_end.state.reshape(2, self.dimension)
        )
        if not self._check_edge_collision(bridge_start, bridge_end, segment_time, trajectory_info):
            return (bridge_start, bridge_end, bridge_start, bridge_end)  # Treat as bridge collision

        # If no collision, return the full path
        path = self._connect_paths(paths)

        return path
    
    def _check_path_collision(self, path):
        """
        Check if a given path has any collision. Assume the path is begin from the root (start or goal) to the leaf

        Args:
            path (list of Node): A path represented as a list of Node objects.

        Returns:
            tuple or None:
                - (collision_segment_start, collision_segment_end): Start and end nodes of the colliding segment.
                - None if the path is collision-free.
        """
        for i in range(len(path) - 1):
            start_node, end_node = path[i], path[i + 1]
            if not self._check_edge_collision(
                start_node, end_node, end_node.segment_time, end_node.trajectory_info
            ):
                return (start_node, end_node)  # Return the colliding segment
        return None  # Path is collision-free

    def _check_edge_collision(self, start_node, end_node, segment_time, trajectory_info, time_step=0.01):
        """
        Check if a single edge (segment) has a collision.

        Args:
            start_node (Node): The starting node of the segment.
            end_node (Node): The ending node of the segment.
            segment_time (float): Duration of the segment.
            trajectory_info (object): Trajectory-related information for the segment.
            time_step (float): Time step for sampling along the trajectory.

        Returns:
            bool: True if the edge is collision-free, False otherwise.
        """
        if segment_time == 0:
            return True

        # Generate time points to sample along the trajectory
        num_samples = int(segment_time / time_step) + 1
        times = np.linspace(0, segment_time, num_samples)

        # Check each sampled point
        for t in times:
            position, velocity = self._get_state_in_segment(
                start_state=start_node.state.reshape(2, self.dimension),
                end_state=end_node.state.reshape(2, self.dimension),
                segment_time=segment_time,
                segment_trajectory=trajectory_info,
                t=t,
            )
            # Collision check at this position
            if not self.collision_checker(position):
                return False  # Collision detected

        return True  # No collision along the segment

    ############### Visualization ###############
    def _initialize_plot(self):
        """
        Initialize the plot with basic elements like start, goal, and obstacles.
        """
        # Set limits
        self.ax.set_xlim(self.position_limits[0][0], self.position_limits[1][0])
        self.ax.set_ylim(self.position_limits[0][1], self.position_limits[1][1])
        self.ax.set_title("Ramp Planner Visualization")
        self.ax.set_xlabel("Position X")
        self.ax.set_ylabel("Position Y")

        # Add obstacles if provided
        if self.visualization_args and "obstacles" in self.visualization_args:
            # Check if the obstacle label has already been added
            if not hasattr(self, "_obstacle_label_added"):
                self._obstacle_label_added = False
            
            for obs in self.visualization_args["obstacles"]:
                x, y, width, height = obs
                # Add the label "Obstacle" only once
                if not self._obstacle_label_added:
                    self.ax.add_patch(plt.Rectangle((x, y), width, height, color="gray", alpha=0.5, label="Obstacle"))
                    self._obstacle_label_added = True
                else:
                    self.ax.add_patch(plt.Rectangle((x, y), width, height, color="gray", alpha=0.5))

        # Draw start node
        self.ax.scatter(self.start.state[0], self.start.state[1], c='green', label="Start")
        self._draw_velocity_arrow(self.start.state[:2], self.start.state[2:], 'green')  # Start velocity

        self._draw_node(self.forward_tree[0], color='g')
        self._draw_edge(self.forward_tree[0], self.start, color='green')

        # Draw goal node
        self.ax.scatter(self.goal.state[0], self.goal.state[1], c='red', label="Goal")
        self._draw_velocity_arrow(self.goal.state[:2], self.goal.state[2:], 'red')  # Goal velocity

        self._draw_node(self.backward_tree[0], color='r')
        self._draw_edge(self.backward_tree[0], self.goal, color='red')

        self.ax.legend(loc="upper left")

        self.iteration_text = self.ax.text(0.95, 0.95, f"Num of nodes: {2+len(self.forward_tree)+len(self.backward_tree)}", 
                                            transform=self.ax.transAxes, 
                                            fontsize=12, color="blue",
                                            ha="right", va="top")

        plt.show(block=False)
        plt.pause(0.5)

    def _draw_velocity_arrow(self, position, velocity, color):
        """
        Draw a velocity arrow at the given position.

        Args:
            ax (matplotlib.axes.Axes): The axis to draw on.
            position (np.ndarray): The position as [x, y].
            velocity (np.ndarray): The velocity as [vx, vy].
            color (str): The color of the arrow, matching the point color.
        """
        if velocity[0] == 0 and velocity[1] == 0:
            return
        # Scale arrow length for better visualization
        arrow_scale = 0.5  # Adjust as needed for visualization clarity
        self.ax.arrow(
            position[0], position[1],      # Starting point of the arrow
            velocity[0] * arrow_scale,     # Scaled x component of the velocity
            velocity[1] * arrow_scale,     # Scaled y component of the velocity
            head_width=0.15,                # Width of the arrowhead
            head_length=0.15,               # Length of the arrowhead
            fc=color, ec=color, alpha=0.2  # Face and edge color
        )

    def _update_plot(self):
        """
        Dynamically update the plot by adding new edges, removing invalid edges,
        and updating nodes without clearing the entire canvas.
        """
        # Initialize edge dictionary if not already done
        if not hasattr(self, "edges"):
            self.edges = {"forward": {}, "backward": {}}  # Track edges for forward and backward trees

        # Update edges and nodes for each tree
        for tree_type, tree, color in [
            ("forward", self.forward_tree, 'g'),
            ("backward", self.backward_tree, 'r'),
        ]:
            # Set of currently valid edges in the tree (use frozenset for undirected edges)
            current_edges = set()

            for node in tree:
                if node.parent is None:  # Skip root node
                    continue

                # Draw the node and velocity arrow
                self._draw_node(node, color)

                # Get parent node
                parent_node = node.parent

                # Generate a unique key for the edge (undirected)
                edge_key = (frozenset({parent_node.uuid, node.uuid}))
                current_edges.add(edge_key)

                # Check if the edge needs to be added
                if edge_key not in self.edges[tree_type]:
                    # New edge, draw it
                    if node.segment_time is not None and node.trajectory_info is not None:
                        line_obj = self._draw_edge(
                            parent_node, node, color=f'{color}-'
                        )
                        self.edges[tree_type][edge_key] = line_obj  # Save the Line2D object

            # Remove edges that are no longer valid in the tree
            invalid_edges = set(self.edges[tree_type].keys()) - current_edges
            for edge_key in invalid_edges:
                # Remove the Line2D object from the canvas
                self.edges[tree_type][edge_key].remove()
                # Delete from edge tracking dictionary
                del self.edges[tree_type][edge_key]

        if self.smoothed_path is not None:
            self._draw_path(path=self.smoothed_path, color='orange', label='Smoothed Path')
        elif self.path is not None:
            self._draw_path(path=self.path, color='blue', label='Path')
        
        # Add legend if not already added
        if not hasattr(self, "_legend_added") or not self._legend_added:
            handles, labels = self.ax.get_legend_handles_labels()
            if len(labels) <= 10:
                self.ax.legend(loc="upper left")
            self._legend_added = True  # Mark that the legend has been added

        self.iteration_text.set_text(f"Num of nodes: {2+len(self.forward_tree)+len(self.backward_tree)}")

        plt.show(block=False)
        plt.pause(0.5)

    def _draw_node(self, node, color):
        """
        Plot a node and its velocity arrow.

        Args:
            node (Node): The node to plot.
            color (str): The color for the node and arrow.
        """
        pos, vel = node.state[:2], node.state[2:]
        self.ax.plot(pos[0], pos[1], f'{color}o', markersize=3)
        self._draw_velocity_arrow(position=pos, velocity=vel, color=color)

    def _draw_edge(self, from_node, to_node, color, linewidth=0.5):
        """
        Draw the trajectory segment connecting two nodes.

        Args:
            from_node (Node): The starting node of the trajectory.
            to_node (Node): The ending node of the trajectory.
            color (str): The color for the trajectory line.
            linewidth (float): The width of the trajectory line.
        """
        if to_node.segment_time==0:
            return  # Skip if trajectory is not meaningful.

        positions, velocities = [], []
        times = np.linspace(0, np.sum(to_node.segment_time), 10)  # Time steps for visualization

        for t in times:
            state = self._get_state_in_segment(
                start_state=from_node.state.reshape(2, self.dimension),
                end_state=to_node.state.reshape(2, self.dimension),
                segment_time=to_node.segment_time,
                segment_trajectory=to_node.trajectory_info,
                t=t
            )
            positions.append(state[0])
            velocities.append(state[1])

        positions = np.array(positions)
        line_obj, = self.ax.plot(positions[:, 0], positions[:, 1], color, linewidth=linewidth)

        return line_obj
    
    def _draw_path(self, path, color='blue', label='Path', point_size=5, linewidth=1.5):
        """
        Visualize the path on the plot using _draw_edge, and draw the nodes (points) along the path.

        Args:
            path: The path to draw.
            color (str): The color and style of the path line (default is 'blue').
            label (str): The label of the path line (default is 'Path').
            point_size (float): The size of the points (default is 5).
            linewidth (float): The width of the path line (default is 1.5).
        """    
        # Iterate through consecutive nodes in the path
        for i in range(len(path)):
            current_node = path[i]

            # Draw the point (node)
            self.ax.scatter(
                current_node.state[0], current_node.state[1], 
                c=color, s=point_size, label=label if i == 0 else None
            )

            # Draw the edge connecting this node to the previous one
            if i > 0:
                start_node = path[i - 1]
                end_node = current_node

                if end_node.segment_time is None or end_node.trajectory_info is None:
                    print(f"Warning: Missing trajectory info for edge ({start_node}, {end_node}). Skipping this edge.")
                    continue

                self._draw_edge(start_node, end_node, color=color, linewidth=linewidth)

        # Add a legend for the path points
        self.ax.legend(loc="upper left")

    ############### Trajectory Generation ###############
    def _get_state_in_segment(self, start_state, end_state, segment_time, segment_trajectory, t):
        """
        Compute the state (position, velocity) within a segment at time t.

        Input:
        - start_state: The starting state of the segment.
        - end_state: The ending state of the segment.
        - segment_time: The duration of the segment.
        - segment_trajectory: Trajectory parameters for each dimension.
        - t: Relative time within the segment.

        Return:
        - position: Numpy array of positions at time t.
        - velocity: Numpy array of velocities at time t.
        """
        position = np.zeros(self.dimension)
        velocity = np.zeros(self.dimension)

        for dim in range(self.dimension):
            acc, trajectory_type = segment_trajectory[dim]
            x1, x2 = start_state[0][dim], end_state[0][dim]
            v1, v2 = start_state[1][dim], end_state[1][dim]

            pos, vel = self._compute_trajectory_state(
                x1=x1, x2=x2, v1=v1, v2=v2, a=acc,
                vmax=self.vmax[dim], T=segment_time,
                trajectory_type=trajectory_type, t=t
            )
            position[dim] = pos
            velocity[dim] = vel

        return position, velocity

    def _compute_trajectory_state(self, x1, x2, v1, v2, a, vmax, T, trajectory_type, t):
        """
        Compute the position x(t) and velocity v(t) for a given trajectory type.

        Input:
        - x1, x2: Initial and final positions.
        - v1, v2: Initial and final velocities.
        - a: Acceleration used in the trajectory.
        - vmax: Maximum velocity.
        - T: Total trajectory time.
        - trajectory_type: One of 'P+P-', 'P-P+', 'P+L+P-', 'P-L-P+'.
        - t: The time at which to compute the state.

        Return:
        - x_t: Position at time t.
        - v_t: Velocity at time t.
        """
        if trajectory_type == 'P+P-':
            # Compute switch time
            t_s = 0.5 * (T + (v2 - v1) / a)
            if t <= t_s:  # Acceleration phase
                v_t = v1 + a * t
                x_t = x1 + v1 * t + 0.5 * a * t**2
            else:  # Deceleration phase
                delta_t = t - t_s
                v_peak = v1 + a * t_s
                v_t = v_peak - a * delta_t
                x_t = (x1 + v1 * t_s + 0.5 * a * t_s**2 +
                    v_peak * delta_t - 0.5 * a * delta_t**2)

        elif trajectory_type == 'P-P+':
            # Compute switch time
            t_s = 0.5 * (T + (v1 - v2) / a)
            if t <= t_s:  # Deceleration phase
                v_t = v1 - a * t
                x_t = x1 + v1 * t - 0.5 * a * t**2
            else:  # Acceleration phase
                delta_t = t - t_s
                v_valley = v1 - a * t_s
                v_t = v_valley + a * delta_t
                x_t = (x1 + v1 * t_s - 0.5 * a * t_s**2 +
                    v_valley * delta_t + 0.5 * a * delta_t**2)

        elif trajectory_type == 'P+L+P-':
            # Compute durations
            t_p1 = (vmax - v1) / a
            t_p2 = (vmax - v2) / a
            t_l = T - t_p1 - t_p2
            if t <= t_p1:  # Acceleration phase
                v_t = v1 + a * t
                x_t = x1 + v1 * t + 0.5 * a * t**2
            elif t <= t_p1 + t_l:  # Constant velocity phase
                delta_t = t - t_p1
                v_t = vmax
                x_t = (x1 + v1 * t_p1 + 0.5 * a * t_p1**2 +
                    vmax * delta_t)
            else:  # Deceleration phase
                delta_t = t - t_p1 - t_l
                v_t = vmax - a * delta_t
                x_t = (x1 + v1 * t_p1 + 0.5 * a * t_p1**2 +
                    vmax * t_l +
                    vmax * delta_t - 0.5 * a * delta_t**2)

        elif trajectory_type == 'P-L-P+':
            # Compute durations
            t_p1 = (vmax + v1) / a
            t_p2 = (vmax + v2) / a
            t_l = T - t_p1 - t_p2
            if t <= t_p1:  # Deceleration phase
                v_t = v1 - a * t
                x_t = x1 + v1 * t - 0.5 * a * t**2
            elif t <= t_p1 + t_l:  # Constant negative velocity phase
                delta_t = t - t_p1
                v_t = -vmax
                x_t = (x1 + v1 * t_p1 - 0.5 * a * t_p1**2 +
                    (-vmax) * delta_t)
            else:  # Acceleration phase
                delta_t = t - t_p1 - t_l
                v_t = -vmax + a * delta_t
                x_t = (x1 + v1 * t_p1 - 0.5 * a * t_p1**2 +
                    (-vmax) * t_l +
                    (-vmax) * delta_t + 0.5 * a * delta_t**2)

        else:
            raise ValueError(f"Unknown trajectory type: {trajectory_type}")

        return x_t, v_t

    def _compute_optimal_segment(self, start_state, end_state):
        segment_time = self._calculate_segment_time(start_state, end_state)
        segment_trajectory = self._calculate_segment_trajectory(start_state, end_state, segment_time)
        
        if segment_time is None or segment_trajectory is None:
            raise ValueError("Invalid trajectory!")
        
        return segment_time, segment_trajectory
    
    def _calculate_segment_time(self, start_state, end_state, safe_margin=1e-6):
        """
        Calculate the maximum time required to traverse a segment across all dimensions,
        considering vmax and amax constraints.
        """
        t_requireds = np.array([
            self._univariate_time_optimal_interpolants(
                start_pos=start_state[0][dim],
                end_pos=end_state[0][dim],
                start_vel=start_state[1][dim],
                end_vel=end_state[1][dim],
                vmax=self.vmax[dim],
                amax=self.amax[dim]
            )
            for dim in range(self.dimension)
        ])
        return np.max(t_requireds) + safe_margin

    def _calculate_segment_trajectory(self, start_state, end_state, segment_time):
        """
        Calculate the trajectory for a single segment using minimum acceleration interpolants.
        """
        # Vectorized calculation for all dimensions
        trajectory_data = [
            self._minimum_acceleration_interpolants(
                start_pos=start_state[0][dim],
                end_pos=end_state[0][dim],
                start_vel=start_state[1][dim],
                end_vel=end_state[1][dim],
                vmax=self.vmax[dim],
                T=segment_time,
                dim=dim,
            )
            for dim in range(self.dimension)
        ]

        # Return None if the trajectory doesn't exist
        if None in trajectory_data:
            return None

        return np.array(trajectory_data, dtype=object)

    def _univariate_time_optimal_interpolants(self, start_pos, end_pos, start_vel, end_vel, vmax, amax):
        """
        Compute the time-optimal trajectory execution time for univariate motion.

        Input:
        - start_pos, end_pos: Initial and final positions.
        - start_vel, end_vel: Initial and final velocities.
        - vmax: Maximum velocity.
        - amax: Maximum acceleration.

        Return:
        - T: Minimal execution time for valid motion primitive combinations, or None if no valid combination exists.
        """
        x1, x2, v1, v2 = start_pos, end_pos, start_vel, end_vel

        def solve_quadratic(a, b, c):
            """Solve quadratic equation ax^2 + bx + c = 0 and return real solutions."""
            discriminant = b**2 - 4 * a * c
            if discriminant < 0:
                return []
            sqrt_discriminant = np.sqrt(discriminant)
            return [(-b + sqrt_discriminant) / (2 * a), (-b - sqrt_discriminant) / (2 * a)]
        
        # Class P+P-
        def compute_p_plus_p_minus():
            coefficients = [amax, 2 * v1, (v1**2 - v2**2) / (2 * amax) + x1 - x2]
            solutions = solve_quadratic(*coefficients)
            valid_t = [t for t in solutions if max((v2 - v1) / amax, 0) <= t <= (vmax - v1) / amax]
            if not valid_t:
                return None
            t_p = valid_t[0]
            
            return np.array(2 * t_p + (v1 - v2) / amax)

        # Class P-P+
        def compute_p_minus_p_plus():
            coefficients = [amax, -2 * v1, (v1**2 - v2**2) / (2 * amax) + x2 - x1]
            solutions = solve_quadratic(*coefficients)
            valid_t = [t for t in solutions if max((v1 - v2) / amax, 0) <= t <= (vmax + v1) / amax]
            if not valid_t:
                return None
            t_p = valid_t[0]
            
            return np.array(2 * t_p + (v2 - v1) / amax)

        # Class P+L+P-
        def compute_p_plus_l_plus_p_minus():
            t_p1 = (vmax - v1) / amax
            t_p2 = (vmax - v2) / amax
            t_l = (v2**2 + v1**2 - 2 * vmax**2) / (2 * vmax * amax) + (x2 - x1) / vmax
            if t_p1 < 0 or t_p2 < 0 or t_l < 0:
                return None
            return np.array(t_p1 + t_l + t_p2)

        # Class P-L+P+
        def compute_p_minus_l_plus_p_plus():
            t_p1 = (vmax + v1) / amax
            t_p2 = (vmax + v2) / amax
            t_l = (v2**2 + v1**2 - 2 * vmax**2) / (2 * vmax * amax) - (x2 - x1) / vmax
            if t_p1 < 0 or t_p2 < 0 or t_l < 0:
                return None
            return np.array(t_p1 + t_l + t_p2)

        # Evaluate all four classes in the specified order
        t_p_plus_p_minus = compute_p_plus_p_minus()
        t_p_minus_p_plus = compute_p_minus_p_plus()
        t_p_plus_l_plus_p_minus = compute_p_plus_l_plus_p_minus()
        t_p_minus_l_plus_p_plus = compute_p_minus_l_plus_p_plus()

        # Collect valid times and return the minimal one
        times = [t for t in [t_p_plus_p_minus, t_p_minus_p_plus, t_p_plus_l_plus_p_minus, t_p_minus_l_plus_p_plus] if t is not None]
        return np.min(times) if times else None
    
    def _minimum_acceleration_interpolants(self, start_pos, end_pos, start_vel, end_vel, vmax, T, dim, t_margin=1e-4, a_margin=1e-6):
        """
        Compute the minimum-acceleration trajectory for fixed end time T.

        Input:
        - start_pos, end_pos: Initial and final positions.
        - start_vel, end_vel: Initial and final velocities.
        - vmax: Maximum velocity.
        - T: Fixed end time.
        - dim: Current dimension.
        - t_margin: A small time margin to compensate for numerical precision errors
        - a_margin: A small acceleration margin to compensate for numerical precision errors

        Return:
        - a_min: Minimal acceleration for valid motion primitive combinations, or None if no valid combination exists.
        - selected_primitive: Name of the selected motion primitive.
        """
        x1, x2, v1, v2 = start_pos, end_pos, start_vel, end_vel

        def solve_quadratic(a, b, c):
            """Solve quadratic equation ax^2 + bx + c = 0 and return real solutions."""
            discriminant = b**2 - 4 * a * c
            if discriminant < 0:
                return []
            sqrt_discriminant = np.sqrt(discriminant)
            return [(-b + sqrt_discriminant) / (2 * a), (-b - sqrt_discriminant) / (2 * a)]

        # Class P+P-
        def compute_p_plus_p_minus():
            coefficients = [T**2, 2 * T * (v1 + v2) + 4 * (x1 - x2), -(v2 - v1)**2]
            solutions = solve_quadratic(*coefficients)
            valid_a = []
            for a in solutions:
                if a <= 0:
                    continue
                t_s = 0.5 * (T + (v2 - v1) / a)
                if 0 < t_s < T + t_margin and abs(v1 + a * t_s) <= vmax:
                    valid_a.append(a)
            return (min(valid_a), 'P+P-') if valid_a else None

        # Class P-P+
        def compute_p_minus_p_plus():
            coefficients = [T**2, -2 * T * (v1 + v2) - 4 * (x1 - x2), -(v2 - v1)**2]
            solutions = solve_quadratic(*coefficients)
            valid_a = []
            for a in solutions:
                if a <= 0:
                    continue
                t_s = 0.5 * (T + (v1 - v2) / a)
                if 0 < t_s < T + t_margin and abs(v1 - a * t_s) <= vmax:
                    valid_a.append(a)
            return (min(valid_a), 'P-P+') if valid_a else None

        # Class P+L+P-
        def compute_p_plus_l_plus_p_minus():
            a = (vmax**2 - vmax * (v1 + v2) + 0.5 * (v1**2 + v2**2)) / (T * vmax - (x2 - x1))
            if a <= 0:
                return None
            t_p1 = (vmax - v1) / a
            t_p2 = (vmax - v2) / a
            t_l = T - t_p1 - t_p2
            if t_p1 < 0 or t_p2 < 0 or t_l < 0:
                return None
            return (a, 'P+L+P-')

        # Class P-L-P+
        def compute_p_minus_l_minus_p_plus():
            a = (vmax**2 + vmax * (v1 + v2) + 0.5 * (v1**2 + v2**2)) / (T * vmax + (x2 - x1))
            if a <= 0:
                return None
            t_p1 = (vmax + v1) / a
            t_p2 = (vmax + v2) / a
            t_l = T - t_p1 - t_p2
            if t_p1 < 0 or t_p2 < 0 or t_l < 0:
                return None
            return (a, 'P-L-P+')

        # Evaluate all four classes independently
        results = [
            compute_p_plus_p_minus(),  # P+P-
            compute_p_minus_p_plus(),  # P-P+
            compute_p_plus_l_plus_p_minus(),  # P+L+P-
            compute_p_minus_l_minus_p_plus()  # P-L-P+
        ]
        valid_results = [result for result in results if result is not None]

        if not valid_results:
            raise ValueError("No valid result")

        # Find the minimum acceleration and corresponding primitive
        a_min, selected_primitive = min(valid_results, key=lambda x: x[0])

        if a_min <= self.amax[dim] + a_margin:
            a_min = np.clip(a_min, 0, self.amax[dim])  
        else: 
            # Return None if the acceleration exceeds the limit
            return None

        return a_min, selected_primitive
