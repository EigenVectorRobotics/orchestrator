# skill_registry

The contract model, the tree validator and live-skill discovery. It reads contracts from skill packages and holds no skill itself.

- **`contract.py`**: Parses and checks a contract: kinds, transports, ports, watches, config.
- **`registry.py`**: All contracts from skill folders, and which are live in the ROS graph.
- **`registry_node.py`**: Publishes the live skills.
- **`validator.py`**: Checks a tree against the contracts: skills, ports, blind mode, places.
- **`tree_files.py`**: Expands subtree includes.
- **`validate_tree.py`**: The validate_tree command.
