from typing import List, Dict, Optional
from pydantic import BaseModel, Field

class FirstTask(BaseModel):
    """
    Details for the first task to be assigned.
    """

    should_create: bool = Field(
        ...,
        description="Whether or not this task should be created.",
    )
    title: Optional[str] = Field(
        ...,
        description="If `should_create is True`, then a suitable title for the task, a few words.",
    )
    description: str = Field(
        ...,
        description="If `should_create is True`, then a suitable description of the task, written in third person, ignoring the chat context (no quotes etc.)",
    )
    start_at: Optional[str] = Field(
        ...,
        description="Optional starting timestamp (YYYY-MM-DDTHH:MM:SS). If unspecified, then the task is started as soon as possible.",
    )
    recurring: Optional[List[str]] = Field(
        ...,
        description="Optional recurring schedule, as a list of days and times (['Monday:HH:MM:SS', 'Wednesday:HH:MM:SS'])",
    )

class FirstTaskResponse(BaseModel):
    """
    Whether or not a task was requested from the user. If so, also return a full description of the requested task.
    """

    reasoning: str = Field(
        ...,
        description="The reasoning behind your decision as to whether or not a task was requested.",
    )
    task_was_requested: bool = Field(
        ...,
        description="You believe a task was requested.",
    )
    first_task: Optional[FirstTask] = Field(
        ...,
        description="Full breakdown of the task specified by the user.",
    )