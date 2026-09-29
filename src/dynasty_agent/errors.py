"""Errors a caller can show the user and carry on from.

The CLI turns an AgentError into a message and exit code 1; the chat shows the
message and keeps the conversation going. Nothing below the CLI raises
SystemExit, so a missing sync or an empty table never ends a chat session.
"""


class AgentError(Exception):
    """A problem with setup or data the user can fix, usually by running a command."""
