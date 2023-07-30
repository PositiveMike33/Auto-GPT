from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Optional

if TYPE_CHECKING:
    from autogpt.config import AIConfig, Config
    from autogpt.llm.base import ChatModelResponse, ChatSequence
    from autogpt.memory.vector import VectorMemory
    from autogpt.models.command_registry import CommandRegistry

from autogpt.json_utils.utilities import extract_dict_from_response, validate_dict
from autogpt.llm.api_manager import ApiManager
from autogpt.llm.base import Message
from autogpt.llm.utils import count_string_tokens
from autogpt.logs import logger
from autogpt.logs.log_cycle import (
    CURRENT_CONTEXT_FILE_NAME,
    FULL_MESSAGE_HISTORY_FILE_NAME,
    NEXT_ACTION_FILE_NAME,
    USER_INPUT_FILE_NAME,
    LogCycleHandler,
)
from autogpt.memory.agent_history import ActionHistory
from autogpt.workspace import Workspace

from .base import AgentThoughts, BaseAgent, CommandArgs, CommandName

PLANNING_AGENT_SYSTEM_PROMPT = """You are an AI agent named {ai_name}"""


class PlanningAgent(BaseAgent):
    """Agent class for interacting with Auto-GPT."""

    ThoughtProcessID = Literal["plan", "action", "evaluate"]

    def __init__(
        self,
        ai_config: AIConfig,
        command_registry: CommandRegistry,
        memory: VectorMemory,
        triggering_prompt: str,
        config: Config,
        cycle_budget: Optional[int] = None,
    ):
        super().__init__(
            ai_config=ai_config,
            command_registry=command_registry,
            config=config,
            default_cycle_instruction=triggering_prompt,
            cycle_budget=cycle_budget,
        )

        self.memory = memory
        """VectorMemoryProvider used to manage the agent's context (TODO)"""

        self.workspace = Workspace(config.workspace_path, config.restrict_to_workspace)
        """Workspace that the agent has access to, e.g. for reading/writing files."""

        self.created_at = datetime.now().strftime("%Y%m%d_%H%M%S")
        """Timestamp the agent was created; only used for structured debug logging."""

        self.log_cycle_handler = LogCycleHandler()
        """LogCycleHandler for structured debug logging."""

        self.action_history = ActionHistory()

        self.plan: list[str] = []
        """List of steps that the Agent plans to take"""

    def construct_base_prompt(
        self, thought_process_id: ThoughtProcessID, **kwargs
    ) -> ChatSequence:
        prepend_messages = kwargs["prepend_messages"] = kwargs.get(
            "prepend_messages", []
        )

        match thought_process_id:
            case "plan" | "action":
                # Add the current plan to the prompt, if any
                if self.plan:
                    plan_section = [
                        "## Plan",
                        "To complete your task, you have made the following plan:",
                    ]
                    plan_section += [f"{i}. {s}" for i, s in enumerate(self.plan, 1)]

                    # Add the actions so far to the prompt
                    if self.action_history:
                        plan_section += [
                            "\n### Progress",
                            "So far, you have executed the following actions based on the plan:",
                        ]
                        for i, cycle in enumerate(self.action_history, 1):
                            if not (cycle.action and cycle.result):
                                logger.warn(f"Incomplete action in history: {cycle}")
                                continue

                            result_description = (
                                "successful"
                                if cycle.result.success
                                else f"not successful, with the following message: {cycle.result.reason}"
                            )
                            plan_section.append(
                                f"{i}. You executed `{cycle.action.format_call()}`: "
                                f"the result was {result_description}."
                            )

                    prepend_messages.append(Message("system", "\n".join(plan_section)))

            case "evaluate":
                pass
            case _:
                raise NotImplementedError(
                    f"Unknown thought process '{thought_process_id}'"
                )

        return super().construct_base_prompt(
            thought_process_id=thought_process_id, **kwargs
        )

    def on_before_think(self, *args, **kwargs) -> ChatSequence:
        prompt = super().on_before_think(*args, **kwargs)

        self.log_cycle_handler.log_count_within_cycle = 0
        self.log_cycle_handler.log_cycle(
            self.ai_config.ai_name,
            self.created_at,
            self.cycle_count,
            self.history.raw(),
            FULL_MESSAGE_HISTORY_FILE_NAME,
        )
        self.log_cycle_handler.log_cycle(
            self.ai_config.ai_name,
            self.created_at,
            self.cycle_count,
            prompt.raw(),
            CURRENT_CONTEXT_FILE_NAME,
        )
        return prompt

    def execute(
        self,
        command_name: str | None,
        command_args: dict[str, str] | None,
        user_input: str | None,
    ) -> str:
        # Execute command
        if command_name is not None and command_name.lower().startswith("error"):
            result = f"Could not execute command: {command_name}{command_args}"
        elif command_name == "human_feedback":
            result = f"Human feedback: {user_input}"
            self.log_cycle_handler.log_cycle(
                self.ai_config.ai_name,
                self.created_at,
                self.cycle_count,
                user_input,
                USER_INPUT_FILE_NAME,
            )

        else:
            for plugin in self.config.plugins:
                if not plugin.can_handle_pre_command():
                    continue
                command_name, arguments = plugin.pre_command(command_name, command_args)
            command_result = execute_command(
                command_name=command_name,
                arguments=command_args,
                agent=self,
            )
            result = f"Command {command_name} returned: " f"{command_result}"

            result_tlength = count_string_tokens(str(command_result), self.llm.name)
            memory_tlength = count_string_tokens(
                str(self.history.summary_message()), self.llm.name
            )
            if result_tlength + memory_tlength > self.send_token_limit:
                result = f"Failure: command {command_name} returned too much output. \
                    Do not execute this command again with the same arguments."

            for plugin in self.config.plugins:
                if not plugin.can_handle_post_command():
                    continue
                result = plugin.post_command(command_name, result)
        # Check if there's a result from the command append it to the message
        if result is None:
            self.history.add("system", "Unable to execute command", "action_result")
        else:
            self.history.add("system", result, "action_result")

        return result

    def parse_and_process_response(
        self, llm_response: ChatModelResponse, *args, **kwargs
    ) -> tuple[CommandName | None, CommandArgs | None, AgentThoughts]:
        if not llm_response.content:
            raise SyntaxError("Assistant response has no text content")

        assistant_reply_dict = extract_dict_from_response(llm_response.content)

        valid, errors = validate_dict(assistant_reply_dict, self.config)
        if not valid:
            raise SyntaxError(
                "Validation of response failed:\n  "
                + ";\n  ".join([str(e) for e in errors])
            )

        for plugin in self.config.plugins:
            if not plugin.can_handle_post_planning():
                continue
            assistant_reply_dict = plugin.post_planning(assistant_reply_dict)

        response = None, None, assistant_reply_dict

        # Print Assistant thoughts
        if assistant_reply_dict != {}:
            # Get command name and arguments
            try:
                command_name, arguments = extract_command(
                    assistant_reply_dict, llm_response, self.config
                )
                response = command_name, arguments, assistant_reply_dict
            except Exception as e:
                logger.error("Error: \n", str(e))

        self.log_cycle_handler.log_cycle(
            self.ai_config.ai_name,
            self.created_at,
            self.cycle_count,
            assistant_reply_dict,
            NEXT_ACTION_FILE_NAME,
        )
        return response


def extract_command(
    assistant_reply_json: dict, assistant_reply: ChatModelResponse, config: Config
) -> tuple[str, dict[str, str]]:
    """Parse the response and return the command name and arguments

    Args:
        assistant_reply_json (dict): The response object from the AI
        assistant_reply (ChatModelResponse): The model response from the AI
        config (Config): The config object

    Returns:
        tuple: The command name and arguments

    Raises:
        json.decoder.JSONDecodeError: If the response is not valid JSON

        Exception: If any other error occurs
    """
    if config.openai_functions:
        if assistant_reply.function_call is None:
            return "Error:", {"message": "No 'function_call' in assistant reply"}
        assistant_reply_json["command"] = {
            "name": assistant_reply.function_call.name,
            "args": json.loads(assistant_reply.function_call.arguments),
        }
    try:
        if "command" not in assistant_reply_json:
            return "Error:", {"message": "Missing 'command' object in JSON"}

        if not isinstance(assistant_reply_json, dict):
            return (
                "Error:",
                {
                    "message": f"The previous message sent was not a dictionary {assistant_reply_json}"
                },
            )

        command = assistant_reply_json["command"]
        if not isinstance(command, dict):
            return "Error:", {"message": "'command' object is not a dictionary"}

        if "name" not in command:
            return "Error:", {"message": "Missing 'name' field in 'command' object"}

        command_name = command["name"]

        # Use an empty dictionary if 'args' field is not present in 'command' object
        arguments = command.get("args", {})

        return command_name, arguments
    except json.decoder.JSONDecodeError:
        return "Error:", {"message": "Invalid JSON"}
    # All other errors, return "Error: + error message"
    except Exception as e:
        return "Error:", {"message": str(e)}


def execute_command(
    command_name: str,
    arguments: dict[str, str],
    agent: PlanningAgent,
) -> Any:
    """Execute the command and return the result

    Args:
        command_name (str): The name of the command to execute
        arguments (dict): The arguments for the command
        agent (Agent): The agent that is executing the command

    Returns:
        str: The result of the command
    """
    try:
        # Execute a native command with the same name or alias, if it exists
        if command := agent.command_registry.get_command(command_name):
            return command(**arguments, agent=agent)

        # Handle non-native commands (e.g. from plugins)
        for command in agent.ai_config.prompt_generator.commands:
            if (
                command_name == command.label.lower()
                or command_name == command.name.lower()
            ):
                return command.function(**arguments)

        raise RuntimeError(
            f"Cannot execute '{command_name}': unknown command."
            " Do not try to use this command again."
        )
    except Exception as e:
        return f"Error: {str(e)}"