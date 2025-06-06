import os
import threading
import time
from abc import ABC, abstractmethod

import markdown
import schedule

from mmrelay.config import get_plugin_data_dir
from mmrelay.db_utils import (
    delete_plugin_data,
    get_plugin_data,
    get_plugin_data_for_node,
    store_plugin_data,
)
from mmrelay.log_utils import get_logger

# Global config variable that will be set from main.py
config = None


class BasePlugin(ABC):
    """Abstract base class for all mmrelay plugins.

    Provides common functionality for plugin development including:
    - Configuration management and validation
    - Database storage for plugin-specific data
    - Channel and direct message handling
    - Matrix message sending capabilities
    - Scheduling support for background tasks
    - Command matching and routing

    Attributes:
        plugin_name (str): Unique identifier for the plugin
        max_data_rows_per_node (int): Maximum data rows stored per node (default: 100)
        priority (int): Plugin execution priority (lower = higher priority, default: 10)

    Subclasses must:
    - Set plugin_name as a class attribute
    - Implement handle_meshtastic_message() and handle_room_message()
    - Optionally override other methods for custom behavior
    """

    # Class-level default attributes
    plugin_name = None  # Must be overridden in subclasses
    max_data_rows_per_node = 100
    priority = 10

    @property
    def description(self):
        """Get the plugin description for help text.

        Returns:
            str: Human-readable description of plugin functionality

        Override this property in subclasses to provide meaningful help text
        that will be displayed by the help plugin.
        """
        return ""

    def __init__(self, plugin_name=None) -> None:
        """Initialize the plugin with configuration and logging.

        Args:
            plugin_name (str, optional): Plugin name override. If not provided,
                                       uses class-level plugin_name attribute.

        Raises:
            ValueError: If plugin_name is not set via parameter or class attribute

        Sets up:
        - Plugin-specific logger
        - Configuration from global config
        - Channel mapping and validation
        - Response delay settings
        """
        # Allow plugin_name to be passed as a parameter for simpler initialization
        # This maintains backward compatibility while providing a cleaner API
        super().__init__()

        # If plugin_name is provided as a parameter, use it
        if plugin_name is not None:
            self.plugin_name = plugin_name

        # For backward compatibility: if plugin_name is not provided as a parameter,
        # check if it's set as an instance attribute (old way) or use the class attribute
        if not hasattr(self, "plugin_name") or self.plugin_name is None:
            # Try to get the class-level plugin_name
            class_plugin_name = getattr(self.__class__, "plugin_name", None)
            if class_plugin_name is not None:
                self.plugin_name = class_plugin_name
            else:
                raise ValueError(
                    f"{self.__class__.__name__} is missing plugin_name definition. "
                    f"Either set class.plugin_name, pass plugin_name to __init__, "
                    f"or set self.plugin_name before calling super().__init__()"
                )

        self.logger = get_logger(f"Plugin:{self.plugin_name}")
        self.config = {"active": False}
        global config
        plugin_levels = ["plugins", "community-plugins", "custom-plugins"]

        # Check if config is available
        if config is not None:
            for level in plugin_levels:
                if level in config and self.plugin_name in config[level]:
                    self.config = config[level][self.plugin_name]
                    break

            # Get the list of mapped channels
            self.mapped_channels = [
                room.get("meshtastic_channel")
                for room in config.get("matrix_rooms", [])
            ]
        else:
            self.mapped_channels = []

        # Get the channels specified for this plugin, or default to all mapped channels
        self.channels = self.config.get("channels", self.mapped_channels)

        # Ensure channels is a list
        if not isinstance(self.channels, list):
            self.channels = [self.channels]

        # Validate the channels
        invalid_channels = [
            ch for ch in self.channels if ch not in self.mapped_channels
        ]
        if invalid_channels:
            self.logger.warning(
                f"Plugin '{self.plugin_name}': Channels {invalid_channels} are not mapped in configuration."
            )

        # Get the response delay from the meshtastic config only
        self.response_delay = 3  # Default value
        if config is not None:
            self.response_delay = config.get("meshtastic", {}).get(
                "plugin_response_delay", self.response_delay
            )

    def start(self):
        """Start the plugin and set up scheduled tasks if configured.

        Called automatically when the plugin is loaded. Checks plugin configuration
        for scheduling settings and sets up background jobs accordingly.

        Supported schedule formats in config:
        - schedule.hours + schedule.at: Run every N hours at specific time
        - schedule.minutes + schedule.at: Run every N minutes at specific time
        - schedule.hours: Run every N hours
        - schedule.minutes: Run every N minutes

        Creates a daemon thread to run the scheduler if any schedule is configured.
        """
        if "schedule" not in self.config or (
            "at" not in self.config["schedule"]
            and "hours" not in self.config["schedule"]
            and "minutes" not in self.config["schedule"]
        ):
            self.logger.debug(f"Started with priority={self.priority}")
            return

        # Schedule the background job based on the configuration
        if "at" in self.config["schedule"] and "hours" in self.config["schedule"]:
            schedule.every(self.config["schedule"]["hours"]).hours.at(
                self.config["schedule"]["at"]
            ).do(self.background_job)
        elif "at" in self.config["schedule"] and "minutes" in self.config["schedule"]:
            schedule.every(self.config["schedule"]["minutes"]).minutes.at(
                self.config["schedule"]["at"]
            ).do(self.background_job)
        elif "hours" in self.config["schedule"]:
            schedule.every(self.config["schedule"]["hours"]).hours.do(
                self.background_job
            )
        elif "minutes" in self.config["schedule"]:
            schedule.every(self.config["schedule"]["minutes"]).minutes.do(
                self.background_job
            )

        # Function to execute the scheduled tasks
        def run_schedule():
            while True:
                schedule.run_pending()
                time.sleep(1)

        # Create a thread for executing the scheduled tasks
        schedule_thread = threading.Thread(target=run_schedule)

        # Start the thread
        schedule_thread.start()
        self.logger.debug(f"Scheduled with priority={self.priority}")

    # trunk-ignore(ruff/B027)
    def background_job(self):
        """Background task executed on schedule.

        Override this method in subclasses to implement scheduled functionality.
        Called automatically based on schedule configuration in start().

        Default implementation does nothing.
        """
        pass  # Implement in subclass if needed

    def strip_raw(self, data):
        """Recursively remove 'raw' keys from data structures.

        Args:
            data: Data structure (dict, list, or other) to clean

        Returns:
            Cleaned data structure with 'raw' keys removed

        Useful for cleaning packet data before logging or storage to remove
        binary protobuf data that's not human-readable.
        """
        if isinstance(data, dict):
            data.pop("raw", None)
            for k, v in data.items():
                data[k] = self.strip_raw(v)
        elif isinstance(data, list):
            data = [self.strip_raw(item) for item in data]
        return data

    def get_response_delay(self):
        """Get the configured response delay for meshtastic messages.

        Returns:
            int: Delay in seconds before sending responses (default: 3)

        Used to prevent message flooding and ensure proper radio etiquette.
        Delay is configured via meshtastic.plugin_response_delay in config.
        """
        return self.response_delay

    def is_channel_enabled(self, channel, is_direct_message=False):
        """Check if the plugin should respond on a specific channel.

        Args:
            channel: Channel identifier to check
            is_direct_message (bool): Whether this is a direct message

        Returns:
            bool: True if plugin should respond, False otherwise

        Direct messages always return True if the plugin is active.
        For channel messages, checks if channel is in plugin's configured channels list.
        """
        if is_direct_message:
            return True  # Always respond to DMs if the plugin is active
        else:
            return channel in self.channels

    def get_matrix_commands(self):
        """Get list of Matrix commands this plugin responds to.

        Returns:
            list: List of command strings (without ! prefix)

        Default implementation returns [plugin_name]. Override to provide
        custom commands or multiple command aliases.
        """
        return [self.plugin_name]

    async def send_matrix_message(self, room_id, message, formatted=True):
        """Send a message to a Matrix room.

        Args:
            room_id (str): Matrix room identifier
            message (str): Message content to send
            formatted (bool): Whether to send as formatted HTML (default: True)

        Returns:
            dict: Response from Matrix API room_send

        Connects to Matrix using matrix_utils and sends a room message
        with optional HTML formatting via markdown.
        """
        from mmrelay.matrix_utils import connect_matrix

        matrix_client = await connect_matrix()

        return await matrix_client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "format": "org.matrix.custom.html" if formatted else None,
                "body": message,
                "formatted_body": markdown.markdown(message) if formatted else None,
            },
        )

    def get_mesh_commands(self):
        """Get list of mesh/radio commands this plugin responds to.

        Returns:
            list: List of command strings (without ! prefix)

        Default implementation returns empty list. Override to handle
        commands sent over the mesh radio network.
        """
        return []

    def store_node_data(self, meshtastic_id, node_data):
        """Store data for a specific node, appending to existing data.

        Args:
            meshtastic_id (str): Node identifier
            node_data: Data to store (single item or list)

        Retrieves existing data, appends new data, trims to max_data_rows_per_node,
        and stores back to database. Use for accumulating time-series data.
        """
        data = self.get_node_data(meshtastic_id=meshtastic_id)
        data = data[-self.max_data_rows_per_node :]
        if isinstance(node_data, list):
            data.extend(node_data)
        else:
            data.append(node_data)
        store_plugin_data(self.plugin_name, meshtastic_id, data)

    def set_node_data(self, meshtastic_id, node_data):
        """Replace all data for a specific node.

        Args:
            meshtastic_id (str): Node identifier
            node_data: Data to store (replaces existing data)

        Completely replaces existing data for the node, trimming to
        max_data_rows_per_node if needed. Use when you want to reset
        or completely replace a node's data.
        """
        node_data = node_data[-self.max_data_rows_per_node :]
        store_plugin_data(self.plugin_name, meshtastic_id, node_data)

    def delete_node_data(self, meshtastic_id):
        """Delete all stored data for a specific node.

        Args:
            meshtastic_id (str): Node identifier

        Returns:
            bool: True if deletion succeeded, False otherwise
        """
        return delete_plugin_data(self.plugin_name, meshtastic_id)

    def get_node_data(self, meshtastic_id):
        """Retrieve stored data for a specific node.

        Args:
            meshtastic_id (str): Node identifier

        Returns:
            list: Stored data for the node (JSON deserialized)
        """
        return get_plugin_data_for_node(self.plugin_name, meshtastic_id)

    def get_data(self):
        """Retrieve all stored data for this plugin across all nodes.

        Returns:
            list: List of tuples containing raw data entries

        Returns raw data without JSON deserialization. Use get_node_data()
        for individual node data that's automatically deserialized.
        """
        return get_plugin_data(self.plugin_name)

    def get_plugin_data_dir(self, subdir=None):
        """
        Returns the directory for storing plugin-specific data files.

        Creates the directory if it doesn't exist.

        Args:
            subdir (str, optional): Optional subdirectory within the plugin's data directory.
                                   If provided, this subdirectory will be created.

        Returns:
            str: Path to the plugin's data directory or subdirectory

        Example:
            self.get_plugin_data_dir() returns ~/.mmrelay/data/plugins/your_plugin_name/
            self.get_plugin_data_dir("data_files") returns ~/.mmrelay/data/plugins/your_plugin_name/data_files/
        """
        # Get the plugin-specific data directory
        plugin_dir = get_plugin_data_dir(self.plugin_name)

        # If a subdirectory is specified, create and return it
        if subdir:
            subdir_path = os.path.join(plugin_dir, subdir)
            os.makedirs(subdir_path, exist_ok=True)
            return subdir_path

        return plugin_dir

    def matches(self, event):
        """Check if a Matrix event matches this plugin's commands.

        Args:
            event: Matrix room event to check

        Returns:
            bool: True if event matches plugin commands, False otherwise

        Uses bot_command() utility to check if the event contains any of
        the plugin's matrix commands with proper bot command syntax.
        """
        from mmrelay.matrix_utils import bot_command

        # Pass the entire event to bot_command
        return bot_command(self.plugin_name, event)

    @abstractmethod
    async def handle_meshtastic_message(
        self, packet, formatted_message, longname, meshnet_name
    ):
        pass  # Implement in subclass

    @abstractmethod
    async def handle_room_message(self, room, event, full_message):
        pass  # Implement in subclass
