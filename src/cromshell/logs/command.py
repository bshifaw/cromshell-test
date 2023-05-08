import json
import logging
import os

import click
from termcolor import colored
import requests

import cromshell.utilities.http_utils as http_utils
import cromshell.utilities.io_utils as io_utils
from cromshell.metadata import command as metadata_command
from cromshell.utilities import command_setup_utils

LOGGER = logging.getLogger(__name__)


@click.command(name="logs")
@click.argument("workflow_ids", required=True, nargs=-1)
@click.option(
    "-p",
    "--print-logs",
    is_flag=True,
    default=False,
    help="Print the contents of the logs to stdout if true. "
    "Note: This assumes GCS bucket logs with default permissions otherwise this may not work",
)
@click.option(
    "-f",
    "--fetch-logs",
    is_flag=True,
    default=False,
    help="Download the logs to the current directory if true. "
)
@click.option(
    "-des",
    "--dont-expand-subworkflows",
    is_flag=True,
    default=False,
    help="Do not expand subworkflow info in metadata",
)
@click.option(
    "-j",
    "--json-summary",
    is_flag=True,
    default=False,
    help="Print a json summary of logs, including non-file types.",
)
@click.option(
    "-s",
    "--status",
    default="Failed",
    help="Return a list with links to the task logs with the indicated status. "
    "Separate multiple keys by comma or use 'ALL' to print all logs. "
    "Some standard Cromwell status options are 'ALL', 'Done', 'RetryableFailure', 'Running', and 'Failed'.",
)
@click.pass_obj
def main(
    config,
    workflow_ids: list,
    json_summary: bool,
    status: list,
    dont_expand_subworkflows: bool,
    print_logs: bool,
    fetch_logs: bool,
):
    """Get a subset of the workflow metadata."""

    LOGGER.info("logs")

    return_code = 0

    # If no keys were provided then set key_param to empty else
    # strip trailing comma from keys and split keys by comma
    status_param = (
        ["ALL"]
        if "ALL".lower() in status.lower()
        else str(status).strip(",").split(",")
    )

    LOGGER.info("Status keys set to %s", status_param)

    for workflow_id in workflow_ids:

        command_setup_utils.resolve_workflow_id_and_server(
            workflow_id=workflow_id, cromshell_config=config
        )

        # if not detailed:
        #     workflow_logs = get_workflow_level_logs(config).get("calls")
        #
        #     if json_summary:
        #         io_utils.pretty_print_json(format_json=workflow_logs)
        # else:
        task_logs = get_task_level_outputs(
            config,
            requested_status=status_param,
            expand_subworkflows=not dont_expand_subworkflows
        )

        if fetch_logs:
            download_task_level_logs(all_task_log_metadata=task_logs)
        else:
            if json_summary:
                io_utils.pretty_print_json(format_json=task_logs)
            else:
                print_task_level_logs(
                    all_task_log_metadata=task_logs, cat_logs=print_logs
                )

    return return_code


def get_workflow_level_logs(config) -> dict:
    """Get the workflow level logs from the workflow metadata

    Args:
        config (dict): The cromshell config object
    """

    requests_out = requests.get(
        f"{config.cromwell_api_workflow_id}/logs",
        timeout=config.requests_connect_timeout,
        verify=config.requests_verify_certs,
        headers=http_utils.generate_headers(config),
    )

    if requests_out.ok:
        # check_for_empty_logs(requests_out.json().get("outputs"), config.workflow_id)
        return requests_out.json()
    else:
        http_utils.check_http_request_status_code(
            short_error_message="Failed to retrieve logs for "
            f"workflow: {config.workflow_id}",
            response=requests_out,
            # Raising exception is set false to allow
            # command to retrieve outputs of remaining workflows.
            raise_exception=False,
        )


def get_task_level_outputs(config, expand_subworkflows, requested_status) -> dict:
    """Get the task level outputs from the workflow metadata

    Args:
        config (dict): The cromshell config object
        :param expand_subworkflows: Whether to expand subworkflows
    """
    # Get metadata
    formatted_metadata_parameter = metadata_command.format_metadata_params(
        list_of_keys=config.METADATA_KEYS_TO_OMIT,
        exclude_keys=True,
        expand_subworkflows=expand_subworkflows,
    )

    workflow_metadata = metadata_command.get_workflow_metadata(
        meta_params=formatted_metadata_parameter,
        api_workflow_id=config.cromwell_api_workflow_id,
        timeout=config.requests_connect_timeout,
        verify_certs=config.requests_verify_certs,
        headers=http_utils.generate_headers(config),
    )

    return filter_task_logs_from_workflow_metadata(workflow_metadata=workflow_metadata, requested_status=requested_status)


def filter_task_logs_from_workflow_metadata(
        workflow_metadata: dict, requested_status: list
) -> dict:
    """Get the logs from the workflow metadata

    Args:
        workflow_metadata (dict): The workflow metadata
        requested_status (list): The list of requested status
    """
    calls_metadata = workflow_metadata["calls"]
    all_task_logs = {}

    for call, index_list in calls_metadata.items():
        if "subWorkflowMetadata" in calls_metadata[call][0]:
            all_task_logs[call] = []
            for scatter in calls_metadata[call]:
                all_task_logs[call].append(
                    filter_task_logs_from_workflow_metadata(
                        scatter["subWorkflowMetadata"], requested_status=requested_status
                    )
                )
        else:
            all_task_logs[call] = []
            for index in index_list:
                if "ALL" in requested_status or index.get("executionStatus") in requested_status:
                    all_task_logs[call].append(
                        {
                            "attempt": index.get("attempt"),
                            "backendLogs": get_backend_logs(task_instance=index),
                            "backend": index.get("backend"),
                            "executionStatus": index.get("executionStatus"),
                            "shardIndex": index.get("shardIndex"),
                            "stderr": index.get("stderr"),
                            "stdout": index.get("stdout")
                         },
                    )

    check_for_empty_logs(
        workflow_logs=all_task_logs,
        workflow_id=workflow_metadata["id"],
        requested_status=requested_status
    )

    return all_task_logs


def print_task_level_logs(all_task_log_metadata: dict, cat_logs: bool) -> None:
    """Print the logs from the workflow metadata
    task_logs_metadata: {call_name:[index1{task_log_name: taskvalue}, index2{...}, ...], call_name:[], ...}

    Args:
        all_task_log_metadata (dict): All task logs metadata from the workflow
    """

    for call, index_list in all_task_log_metadata.items():

        print(f"{call}:")
        for call_index in index_list:
            if call_index is not None:
                print_file_like_value_in_dict(
                    task_log_metadata=call_index, indent=1, cat_logs=cat_logs
                )


def print_file_like_value_in_dict(task_log_metadata: dict, indent: int, cat_logs: bool) -> None:
    """Print the file like values in the output metadata dictionary

    Args:
        task_log_metadata (dict): The output metadata
        indent (int): The number of tabs to indent the output
    """

    i = "\t" * indent

    task_status_font = io_utils.get_color_for_status_key(
        task_log_metadata.get("executionStatus")) if task_log_metadata.get('executionStatus') else None

    print(
        colored(
            f"{i}status: {task_log_metadata.get('executionStatus')}",
            color=task_status_font
        )
    )

    for log_name, log_value in task_log_metadata.items():
        if isinstance(log_value, str):
            print_output_name_and_file(
                output_name=log_name,
                output_value=log_value,
                indent=indent,
                txt_color=task_status_font,
                cat_logs=cat_logs,
            )
        elif isinstance(log_value, dict):
            print_file_like_value_in_dict(log_value, indent=indent)
        elif isinstance(log_value, list):  # Lists are subworkflows, an item is a task
            print(f"{i}{log_name}:\t")  # Print the subworkflow task name
            for output_value_item in log_value:
                print_file_like_value_in_dict(
                    task_log_metadata=output_value_item,
                    indent=indent+1,
                    cat_logs=cat_logs,
                )


def print_output_name_and_file(
    output_name: str,
        output_value: str,
        indent: int = 0,
        txt_color: str = None,
        cat_logs: bool = False
) -> None:
    """Print the task name and the file name

    Args:
        output_name (str): The task output name
        output_value (str): The task output value
        indent (bool): Whether to indent the output
        cat_logs (bool): Whether to cat the log file
        txt_color (str): The color to use for printing the output. Default is None. """

    i = "\t" * indent

    if isinstance(output_value, str):
        if io_utils.is_path_or_url_like(output_value):
            if cat_logs:
                print_log_file_content(
                    output_name=output_name,
                    output_value=output_value,
                    txt_color=txt_color,
                )
            else:
                print(colored(f"{i}{output_name}: {output_value}", color=txt_color))


def print_log_file_content(output_name: str, output_value: str, txt_color: str = "blue") -> None:
    """Prints output logs and cat the file if possible.

    Args:
        output_name (str): The name of the output log.
        output_value (str): The value of the output log.
        txt_color (str): The color to use for printing the output. Default is "blue".
    """
    term_size = os.get_terminal_size().columns
    print(colored(
            f"{'=' * term_size}\n{output_name}: {output_value}\n{'=' * term_size}",
            color=txt_color,
        )
    )

    file_contents = io_utils.cat_file(output_value)
    if file_contents:
        print(file_contents)
    else:
        print(f"Unable to locate logs at {output_value}.")
    print("\n\n\n")  # Add some space between logs


def check_for_empty_logs(workflow_logs: dict, workflow_id: str, requested_status) -> None:
    """Check if the workflow logs are empty

    Args:
        :param requested_status: The status requested to be filtered
        :param workflow_logs: The workflow logs
        :param workflow_id: The workflow id
    """
    if not workflow_logs:
        LOGGER.error(f"No calls found for workflow: {workflow_id}")
        raise Exception(f"No calls found for workflow: {workflow_id}")

    if "log" not in json.dumps(workflow_logs):
        LOGGER.error(
            f"No log found for workflow: {workflow_id} with status: {requested_status}"
        )
        raise Exception(
            f"No logs found for workflow: {workflow_id} with status: {requested_status}"
        )


def get_backend_logs(task_instance: dict) -> str:
    """
    Gets the backend log for an instance of a task call

    :param task_instance: Metadata info of a task instance
        e.g. (workflow_metadata['calls'][SomeWorkflow.SomeTask][0])
    :return:
    """
    if task_instance.get("backend") == "Local":
        backend_logs = {"log": "Backend Logs Not Available Due to Local Execution"}

    else:
        backend_logs = task_instance.get(
            "backendLogs", {"log": "Backend Logs Not Found"}
        )

    return backend_logs.get("log")


def download_file_like_value_in_dict(task_log_metadata):
    """Download the file like values in the output metadata dictionary"""

    files_to_download = []

    for log_name, log_value in task_log_metadata.items():
        if isinstance(log_value, str):
            if io_utils.is_path_or_url_like(log_value):
                files_to_download.append(log_value)
        elif isinstance(log_value, dict):
            download_file_like_value_in_dict(log_value)
        elif isinstance(log_value, list):  # Lists are subworkflows, an item is a task
            for output_value_item in log_value:
                download_file_like_value_in_dict(
                    task_log_metadata=output_value_item
                )

    flattened_list = [x for sublist in files_to_download for x in sublist]
    io_utils.download_gcs_files(files_to_download, local_dir=os.getcwd())
    # print(files_to_download)


def download_task_level_logs(all_task_log_metadata):
    """Download the logs from the workflow metadata
    task_logs_metadata: {call_name:[index1{task_log_name: taskvalue}, index2{...}, ...], call_name:[], ...}

    Args:
        all_task_log_metadata (dict): All task logs metadata from the workflow
    """

    for call, index_list in all_task_log_metadata.items():

        for call_index in index_list:
            if call_index is not None:
                download_file_like_value_in_dict(
                    task_log_metadata=call_index
                )
