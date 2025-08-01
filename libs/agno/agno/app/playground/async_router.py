import json
from io import BytesIO
from typing import Any, AsyncGenerator, Dict, List, Optional, cast
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from agno.agent.agent import Agent, RunResponse
from agno.app.playground.operator import (
    format_tools,
    get_agent_by_id,
    get_session_title,
    get_session_title_from_team_session,
    get_session_title_from_workflow_session,
    get_team_by_id,
    get_workflow_by_id,
)
from agno.app.playground.schemas import (
    AgentGetResponse,
    AgentModel,
    AgentRenameRequest,
    AgentSessionsResponse,
    MemoryResponse,
    TeamGetResponse,
    TeamRenameRequest,
    TeamSessionResponse,
    WorkflowGetResponse,
    WorkflowRenameRequest,
    WorkflowRunRequest,
    WorkflowSessionResponse,
    WorkflowsGetResponse,
)
from agno.app.playground.utils import process_audio, process_document, process_image, process_video
from agno.media import Audio, Image, Video
from agno.media import File as FileMedia
from agno.memory.agent import AgentMemory
from agno.memory.v2 import Memory
from agno.run.response import RunResponseErrorEvent, RunResponseEvent
from agno.run.team import RunResponseErrorEvent as TeamRunResponseErrorEvent
from agno.run.v2.workflow import WorkflowErrorEvent
from agno.storage.session.agent import AgentSession
from agno.storage.session.team import TeamSession
from agno.storage.session.workflow import WorkflowSession
from agno.team.team import Team
from agno.utils.log import logger
from agno.workflow.v2.workflow import Workflow as WorkflowV2
from agno.workflow.workflow import Workflow


async def chat_response_streamer(
    agent: Agent,
    message: str,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    images: Optional[List[Image]] = None,
    audio: Optional[List[Audio]] = None,
    videos: Optional[List[Video]] = None,
    files: Optional[List[FileMedia]] = None,
) -> AsyncGenerator:
    try:
        run_response = await agent.arun(
            message,
            session_id=session_id,
            user_id=user_id,
            images=images,
            audio=audio,
            videos=videos,
            files=files,
            stream=True,
            stream_intermediate_steps=True,
        )
        async for run_response_chunk in run_response:
            yield run_response_chunk.to_json()
    except Exception as e:
        import traceback

        traceback.print_exc(limit=3)
        error_response = RunResponseErrorEvent(
            content=str(e),
        )
        yield error_response.to_json()
        return


async def agent_acontinue_run_streamer(
    agent: Agent,
    run_id: Optional[str] = None,
    updated_tools: Optional[List] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> AsyncGenerator:
    try:
        continue_response = await agent.acontinue_run(
            run_id=run_id,
            updated_tools=updated_tools,
            session_id=session_id,
            user_id=user_id,
            stream=True,
            stream_intermediate_steps=True,
        )
        async for run_response_chunk in continue_response:
            run_response_chunk = cast(RunResponseEvent, run_response_chunk)
            yield run_response_chunk.to_json()
    except Exception as e:
        import traceback

        traceback.print_exc(limit=3)
        error_response = RunResponseErrorEvent(
            content=str(e),
        )
        yield error_response.to_json()
        return


async def team_chat_response_streamer(
    team: Team,
    message: str,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    images: Optional[List[Image]] = None,
    audio: Optional[List[Audio]] = None,
    videos: Optional[List[Video]] = None,
    files: Optional[List[FileMedia]] = None,
) -> AsyncGenerator:
    try:
        run_response = await team.arun(
            message,
            session_id=session_id,
            user_id=user_id,
            images=images,
            audio=audio,
            videos=videos,
            files=files,
            stream=True,
            stream_intermediate_steps=True,
        )
        async for run_response_chunk in run_response:
            yield run_response_chunk.to_json()
    except Exception as e:
        import traceback

        traceback.print_exc()
        error_response = TeamRunResponseErrorEvent(
            content=str(e),
        )
        yield error_response.to_json()
        return


async def workflow_response_streamer(
    workflow: WorkflowV2,
    body: WorkflowRunRequest,
) -> AsyncGenerator:
    try:
        run_response = await workflow.arun(
            **body.input,
            user_id=body.user_id,
            session_id=body.session_id or str(uuid4()),
            stream=True,
            stream_intermediate_steps=True,
        )
        async for run_response_chunk in run_response:
            yield run_response_chunk.to_json()
    except Exception as e:
        import traceback

        traceback.print_exc(limit=3)
        error_response = WorkflowErrorEvent(
            error=str(e),
        )
        yield error_response.to_json()
        return


def get_async_playground_router(
    agents: Optional[List[Agent]] = None,
    workflows: Optional[List[Workflow]] = None,
    teams: Optional[List[Team]] = None,
    active_app_id: Optional[str] = None,
) -> APIRouter:
    playground_router = APIRouter(prefix="/playground", tags=["Playground"])

    if agents is None and workflows is None and teams is None:
        raise ValueError("Either agents, teams or workflows must be provided.")

    @playground_router.get("/status")
    async def playground_status(app_id: Optional[str] = None):
        if app_id is None:
            return {"playground": "available"}
        else:
            if active_app_id == app_id:
                return {"playground": "available"}
            else:
                raise HTTPException(status_code=404, detail="Playground not available")

    @playground_router.get("/agents", response_model=List[AgentGetResponse])
    async def get_agents():
        agent_list: List[AgentGetResponse] = []
        if agents is None:
            return agent_list

        for agent in agents:
            agent_tools = agent.get_tools(session_id=str(uuid4()), async_mode=True)
            formatted_tools = format_tools(agent_tools)

            name = agent.model.name or agent.model.__class__.__name__ if agent.model else None
            provider = agent.model.provider or agent.model.__class__.__name__ if agent.model else ""
            model_id = agent.model.id if agent.model else None

            # Create an agent_id if its not set on the agent
            if agent.agent_id is None:
                agent.set_agent_id()

            if provider and model_id:
                provider = f"{provider} {model_id}"
            elif name and model_id:
                provider = f"{name} {model_id}"
            elif model_id:
                provider = model_id
            else:
                provider = ""

            if agent.memory:
                memory_dict: Optional[Dict[str, Any]] = {}
                if isinstance(agent.memory, AgentMemory) and agent.memory.db:
                    memory_dict = {"name": agent.memory.db.__class__.__name__}
                elif isinstance(agent.memory, Memory) and agent.memory.db:
                    memory_dict = {"name": "Memory"}
                    if agent.memory.model is not None:
                        memory_dict["model"] = AgentModel(
                            name=agent.memory.model.name,
                            model=agent.memory.model.id,
                            provider=agent.memory.model.provider,
                        )
                    else:
                        memory_dict["model"] = AgentModel(
                            name=name,
                            model=model_id,
                            provider=provider,
                        )

                    if agent.memory.db is not None:
                        memory_dict["db"] = agent.memory.db.__dict__()  # type: ignore

                else:
                    memory_dict = None
            else:
                memory_dict = None

            agent_list.append(
                AgentGetResponse(
                    agent_id=agent.agent_id,
                    name=agent.name,
                    model=AgentModel(
                        name=name,
                        model=model_id,
                        provider=provider,
                    ),
                    add_context=agent.add_context,
                    tools=formatted_tools,
                    memory=memory_dict,
                    storage={"name": agent.storage.__class__.__name__} if agent.storage else None,
                    knowledge={"name": agent.knowledge.__class__.__name__} if agent.knowledge else None,
                    description=agent.description,
                    instructions=agent.instructions,
                )
            )

        return agent_list

    @playground_router.post("/agents/{agent_id}/runs")
    async def create_agent_run(
        agent_id: str,
        message: str = Form(...),
        stream: bool = Form(True),
        monitor: bool = Form(False),
        session_id: Optional[str] = Form(None),
        user_id: Optional[str] = Form(None),
        files: Optional[List[UploadFile]] = File(None),
    ):
        logger.debug(f"AgentRunRequest: {message} {session_id} {user_id} {agent_id}")
        agent = get_agent_by_id(agent_id, agents)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        if session_id is not None and session_id != "":
            logger.debug(f"Continuing session: {session_id}")
        else:
            logger.debug("Creating new session")
            session_id = str(uuid4())

        if monitor:
            agent.monitoring = True
        else:
            agent.monitoring = False

        base64_images: List[Image] = []
        base64_audios: List[Audio] = []
        base64_videos: List[Video] = []
        input_files: List[FileMedia] = []

        if files:
            for file in files:
                logger.info(f"Processing file: {file.content_type}")
                if file.content_type in ["image/png", "image/jpeg", "image/jpg", "image/webp"]:
                    try:
                        base64_image = process_image(file)
                        base64_images.append(base64_image)
                    except Exception as e:
                        logger.error(f"Error processing image {file.filename}: {e}")
                        continue
                elif file.content_type in ["audio/wav", "audio/mp3", "audio/mpeg"]:
                    try:
                        base64_audio = process_audio(file)
                        base64_audios.append(base64_audio)
                    except Exception as e:
                        logger.error(f"Error processing audio {file.filename}: {e}")
                        continue
                elif file.content_type in [
                    "video/x-flv",
                    "video/quicktime",
                    "video/mpeg",
                    "video/mpegs",
                    "video/mpgs",
                    "video/mpg",
                    "video/mpg",
                    "video/mp4",
                    "video/webm",
                    "video/wmv",
                    "video/3gpp",
                ]:
                    try:
                        base64_video = process_video(file)
                        base64_videos.append(base64_video)
                    except Exception as e:
                        logger.error(f"Error processing video {file.filename}: {e}")
                        continue
                else:
                    # Process document files
                    if file.content_type == "application/pdf":
                        from agno.document.reader.pdf_reader import PDFReader

                        contents = await file.read()

                        # If agent has knowledge base, load the document into it
                        if agent.knowledge is not None:
                            pdf_file = BytesIO(contents)
                            pdf_file.name = file.filename
                            file_content = PDFReader().read(pdf_file)
                            agent.knowledge.load_documents(file_content)
                        else:
                            # If no knowledge base, treat as direct file input (similar to cookbook examples)
                            input_files.append(FileMedia(content=contents))

                    elif file.content_type == "text/csv":
                        from agno.document.reader.csv_reader import CSVReader

                        contents = await file.read()

                        # If agent has knowledge base, load the document into it
                        if agent.knowledge is not None:
                            csv_file = BytesIO(contents)
                            csv_file.name = file.filename
                            file_content = CSVReader().read(csv_file)
                            agent.knowledge.load_documents(file_content)
                        else:
                            # If no knowledge base, treat as direct file input
                            input_files.append(FileMedia(content=contents))

                    elif file.content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                        from agno.document.reader.docx_reader import DocxReader

                        contents = await file.read()

                        # If agent has knowledge base, load the document into it
                        if agent.knowledge is not None:
                            docx_file = BytesIO(contents)
                            docx_file.name = file.filename
                            file_content = DocxReader().read(docx_file)
                            agent.knowledge.load_documents(file_content)
                        else:
                            # If no knowledge base, treat as direct file input
                            input_files.append(FileMedia(content=contents))

                    elif file.content_type == "text/plain":
                        from agno.document.reader.text_reader import TextReader

                        contents = await file.read()

                        # If agent has knowledge base, load the document into it
                        if agent.knowledge is not None:
                            text_file = BytesIO(contents)
                            text_file.name = file.filename
                            file_content = TextReader().read(text_file)
                            agent.knowledge.load_documents(file_content)
                        else:
                            # If no knowledge base, treat as direct file input
                            input_files.append(FileMedia(content=contents))

                    elif file.content_type == "application/json":
                        from agno.document.reader.json_reader import JSONReader

                        contents = await file.read()

                        # If agent has knowledge base, load the document into it
                        if agent.knowledge is not None:
                            json_file = BytesIO(contents)
                            json_file.name = file.filename
                            file_content = JSONReader().read(json_file)
                            agent.knowledge.load_documents(file_content)
                        else:
                            # If no knowledge base, treat as direct file input
                            input_files.append(FileMedia(content=contents))
                    else:
                        raise HTTPException(status_code=400, detail="Unsupported file type")

        if stream:
            return StreamingResponse(
                chat_response_streamer(
                    agent,
                    message,
                    session_id=session_id,
                    user_id=user_id,
                    images=base64_images if base64_images else None,
                    audio=base64_audios if base64_audios else None,
                    videos=base64_videos if base64_videos else None,
                    files=input_files if input_files else None,
                ),
                media_type="text/event-stream",
            )
        else:
            run_response = cast(
                RunResponse,
                await agent.arun(
                    message=message,
                    session_id=session_id,
                    user_id=user_id,
                    images=base64_images if base64_images else None,
                    audio=base64_audios if base64_audios else None,
                    videos=base64_videos if base64_videos else None,
                    files=input_files if input_files else None,
                    stream=False,
                ),
            )
            return run_response.to_dict()

    @playground_router.post("/agents/{agent_id}/runs/{run_id}/continue")
    async def continue_agent_run(
        agent_id: str,
        run_id: str,
        tools: str = Form(...),  # JSON string of tools
        session_id: Optional[str] = Form(None),
        user_id: Optional[str] = Form(None),
        stream: bool = Form(True),
    ):
        # Parse the JSON string manually
        try:
            tools_data = json.loads(tools) if tools else None
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON in tools field")

        logger.debug(
            f"AgentContinueRunRequest: run_id={run_id} session_id={session_id} user_id={user_id} agent_id={agent_id}"
        )
        agent = get_agent_by_id(agent_id, agents)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        if session_id is None or session_id == "":
            logger.warning(
                "Continuing run without session_id. This might lead to unexpected behavior if session context is important."
            )
        else:
            logger.debug(f"Continuing run within session: {session_id}")

        # Convert tools dict to ToolExecution objects if provided
        updated_tools = None
        if tools_data:
            try:
                from agno.models.response import ToolExecution

                updated_tools = [ToolExecution.from_dict(tool) for tool in tools_data]
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid structure or content for tools: {str(e)}")

        if stream:
            return StreamingResponse(
                agent_acontinue_run_streamer(
                    agent,
                    run_id=run_id,  # run_id from path
                    updated_tools=updated_tools,
                    session_id=session_id,
                    user_id=user_id,
                ),
                media_type="text/event-stream",
            )
        else:
            run_response_obj = cast(
                RunResponse,
                await agent.acontinue_run(
                    run_id=run_id,  # run_id from path
                    updated_tools=updated_tools,
                    session_id=session_id,
                    user_id=user_id,
                    stream=False,
                ),
            )
            return run_response_obj.to_dict()

    @playground_router.get("/agents/{agent_id}/sessions")
    async def get_all_agent_sessions(agent_id: str, user_id: Optional[str] = Query(None, min_length=1)):
        logger.debug(f"AgentSessionsRequest: {agent_id} {user_id}")
        agent = get_agent_by_id(agent_id, agents)
        if agent is None:
            return JSONResponse(status_code=404, content="Agent not found.")

        if agent.storage is None:
            return JSONResponse(status_code=404, content="Agent does not have storage enabled.")

        agent_sessions: List[AgentSessionsResponse] = []
        all_agent_sessions: List[AgentSession] = agent.storage.get_all_sessions(user_id=user_id, entity_id=agent_id)  # type: ignore
        for session in all_agent_sessions:
            title = get_session_title(session)
            agent_sessions.append(
                AgentSessionsResponse(
                    title=title,
                    session_id=session.session_id,
                    session_name=session.session_data.get("session_name") if session.session_data else None,
                    created_at=session.created_at,
                )
            )
        return agent_sessions

    @playground_router.get("/agents/{agent_id}/sessions/{session_id}")
    async def get_agent_session(agent_id: str, session_id: str, user_id: Optional[str] = Query(None, min_length=1)):
        logger.debug(f"AgentSessionsRequest: {agent_id} {user_id} {session_id}")
        agent = get_agent_by_id(agent_id, agents)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        if agent.storage is None:
            return JSONResponse(status_code=404, content="Agent does not have storage enabled.")

        agent_session: Optional[AgentSession] = agent.storage.read(session_id, user_id)  # type: ignore
        if agent_session is None:
            return JSONResponse(status_code=404, content="Session not found.")

        agent_session_dict = agent_session.to_dict()
        if agent_session.memory is not None:
            runs = agent_session.memory.get("runs")
            if runs is not None:
                first_run = runs[0]
                # This is how we know it is a RunResponse or RunPaused
                if "content" in first_run or first_run.get("is_paused", False) or first_run.get("event") == "RunPaused":
                    agent_session_dict["runs"] = []

                    for run in runs:
                        first_user_message = None
                        for msg in run.get("messages", []):
                            if msg.get("role") == "user" and msg.get("from_history", False) is False:
                                first_user_message = msg
                                break
                        # Remove the memory from the response
                        run.pop("memory", None)
                        agent_session_dict["runs"].append(
                            {
                                "message": first_user_message,
                                "response": run,
                            }
                        )
        return agent_session_dict

    @playground_router.post("/agents/{agent_id}/sessions/{session_id}/rename")
    async def rename_agent_session(agent_id: str, session_id: str, body: AgentRenameRequest):
        agent = get_agent_by_id(agent_id, agents)
        if agent is None:
            return JSONResponse(status_code=404, content=f"couldn't find agent with {agent_id}")

        if agent.storage is None:
            return JSONResponse(status_code=404, content="Agent does not have storage enabled.")

        all_agent_sessions: List[AgentSession] = agent.storage.get_all_sessions(user_id=body.user_id)  # type: ignore
        for session in all_agent_sessions:
            if session.session_id == session_id:
                agent.rename_session(body.name, session_id=session_id)
                return JSONResponse(content={"message": f"successfully renamed session {session.session_id}"})

        return JSONResponse(status_code=404, content="Session not found.")

    @playground_router.delete("/agents/{agent_id}/sessions/{session_id}")
    async def delete_agent_session(agent_id: str, session_id: str, user_id: Optional[str] = Query(None, min_length=1)):
        agent = get_agent_by_id(agent_id, agents)
        if agent is None:
            return JSONResponse(status_code=404, content="Agent not found.")

        if agent.storage is None:
            return JSONResponse(status_code=404, content="Agent does not have storage enabled.")

        all_agent_sessions: List[AgentSession] = agent.storage.get_all_sessions(user_id=user_id, entity_id=agent_id)  # type: ignore
        for session in all_agent_sessions:
            if session.session_id == session_id:
                agent.delete_session(session_id)
                return JSONResponse(content={"message": f"successfully deleted session {session_id}"})

        return JSONResponse(status_code=404, content="Session not found.")

    @playground_router.get("/agents/{agent_id}/memories")
    async def get_agent_memories(agent_id: str, user_id: str = Query(..., min_length=1)):
        agent = get_agent_by_id(agent_id, agents)
        if agent is None:
            return JSONResponse(status_code=404, content="Agent not found.")

        if agent.memory is None:
            return JSONResponse(status_code=404, content="Agent does not have memory enabled.")

        if isinstance(agent.memory, Memory):
            memories = agent.memory.get_user_memories(user_id=user_id)
            return [
                MemoryResponse(memory=memory.memory, topics=memory.topics, last_updated=memory.last_updated)
                for memory in memories
            ]
        else:
            return []

    @playground_router.get("/workflows", response_model=List[WorkflowsGetResponse])
    async def get_workflows():
        if workflows is None:
            return []

        return [
            WorkflowsGetResponse(
                workflow_id=str(workflow.workflow_id),
                name=workflow.name,
                description=workflow.description,
            )
            for workflow in workflows
        ]

    @playground_router.get("/workflows/{workflow_id}", response_model=WorkflowGetResponse)
    async def get_workflow(workflow_id: str):
        workflow = get_workflow_by_id(workflow_id, workflows)
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found")

        if isinstance(workflow, Workflow):
            return WorkflowGetResponse(
                workflow_id=workflow.workflow_id,
                name=workflow.name,
                description=workflow.description,
                parameters=workflow._run_parameters or {},
                storage=workflow.storage.__class__.__name__ if workflow.storage else None,
            )
        else:
            return WorkflowGetResponse(
                workflow_id=workflow.workflow_id,
                name=workflow.name,
                description=workflow.description,
                parameters=workflow.run_parameters,
                storage=workflow.storage.__class__.__name__ if workflow.storage else None,
            )

    @playground_router.post("/workflows/{workflow_id}/runs")
    async def create_workflow_run(workflow_id: str, body: WorkflowRunRequest):
        # Retrieve the workflow by ID
        workflow = get_workflow_by_id(workflow_id, workflows)
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found")

        if body.session_id is not None:
            logger.debug(f"Continuing session: {body.session_id}")
        else:
            logger.debug("Creating new session")

        # Create a new instance of this workflow
        if isinstance(workflow, Workflow):
            new_workflow_instance = workflow.deep_copy(
                update={"workflow_id": workflow_id, "session_id": body.session_id}
            )
            new_workflow_instance.user_id = body.user_id
            new_workflow_instance.session_name = None

            # Return based on the response type
            try:
                if new_workflow_instance._run_return_type == "RunResponse":
                    # Return as a normal response
                    return new_workflow_instance.run(**body.input)
                else:
                    # Return as a streaming response
                    return StreamingResponse(
                        (result.to_json() for result in new_workflow_instance.run(**body.input)),
                        media_type="text/event-stream",
                        headers={
                            "Access-Control-Allow-Origin": "*",
                            "Access-Control-Allow-Methods": "POST, OPTIONS",
                            "Access-Control-Allow-Headers": "Content-Type, Authorization",
                        },
                    )
            except Exception as e:
                # Handle unexpected runtime errors
                raise HTTPException(status_code=500, detail=f"Error running workflow: {str(e)}")
        else:
            # Return based on the response type
            try:
                if body.stream:
                    # Return as a streaming response
                    return StreamingResponse(
                        workflow_response_streamer(workflow, body),
                        media_type="text/event-stream",
                        headers={
                            "Access-Control-Allow-Origin": "*",
                            "Access-Control-Allow-Methods": "POST, OPTIONS",
                            "Access-Control-Allow-Headers": "Content-Type, Authorization",
                        },
                    )
                else:
                    # Return as a normal response
                    return await workflow.arun(
                        **body.input, session_id=body.session_id or str(uuid4()), user_id=body.user_id
                    )
            except Exception as e:
                # Handle unexpected runtime errors
                raise HTTPException(status_code=500, detail=f"Error running workflow: {str(e)}")

    @playground_router.get("/workflows/{workflow_id}/sessions")
    async def get_all_workflow_sessions(workflow_id: str, user_id: Optional[str] = Query(None, min_length=1)):
        # Retrieve the workflow by ID
        workflow = get_workflow_by_id(workflow_id, workflows)
        if not workflow:
            raise HTTPException(status_code=404, detail="Workflow not found")

        # Ensure storage is enabled for the workflow
        if not workflow.storage:
            raise HTTPException(status_code=404, detail="Workflow does not have storage enabled")

        # Retrieve all sessions for the given workflow and user
        try:
            all_workflow_sessions: List[WorkflowSession] = workflow.storage.get_all_sessions(
                user_id=user_id, entity_id=workflow_id
            )  # type: ignore
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error retrieving sessions: {str(e)}")

        # Return the sessions
        workflow_sessions: List[WorkflowSessionResponse] = []
        for session in all_workflow_sessions:
            title = get_session_title_from_workflow_session(session)
            workflow_sessions.append(
                {
                    "title": title,
                    "session_id": session.session_id,
                    "session_name": session.session_data.get("session_name") if session.session_data else None,
                    "created_at": session.created_at,
                }  # type: ignore
            )
        return workflow_sessions

    @playground_router.get("/workflows/{workflow_id}/sessions/{session_id}")
    async def get_workflow_session(
        workflow_id: str, session_id: str, user_id: Optional[str] = Query(None, min_length=1)
    ):
        # Retrieve the workflow by ID
        workflow = get_workflow_by_id(workflow_id, workflows)
        if not workflow:
            raise HTTPException(status_code=404, detail="Workflow not found")

        # Ensure storage is enabled for the workflow
        if not workflow.storage:
            raise HTTPException(status_code=404, detail="Workflow does not have storage enabled")

        # Retrieve the specific session
        try:
            workflow_session = workflow.storage.read(session_id, user_id)  # type: ignore
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error retrieving session: {str(e)}")

        if not workflow_session:
            raise HTTPException(status_code=404, detail="Session not found")

        workflow_session_dict = workflow_session.to_dict()
        if "memory" not in workflow_session_dict:
            workflow_session_dict["memory"] = {"runs": workflow_session_dict.pop("runs", [])}

        return JSONResponse(content=workflow_session_dict)

    @playground_router.post("/workflows/{workflow_id}/sessions/{session_id}/rename")
    async def rename_workflow_session(workflow_id: str, session_id: str, body: WorkflowRenameRequest):
        workflow = get_workflow_by_id(workflow_id, workflows)
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        workflow.session_id = session_id
        workflow.rename_session(body.name)
        return JSONResponse(content={"message": f"successfully renamed workflow {workflow.name}"})

    @playground_router.delete("/workflows/{workflow_id}/sessions/{session_id}")
    async def delete_workflow_session(workflow_id: str, session_id: str):
        workflow = get_workflow_by_id(workflow_id, workflows)
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found")

        workflow.delete_session(session_id)
        return JSONResponse(content={"message": f"successfully deleted workflow {workflow.name}"})

    @playground_router.get("/teams")
    async def get_teams():
        if teams is None:
            return []

        return [TeamGetResponse.from_team(team, async_mode=True) for team in teams]

    @playground_router.get("/teams/{team_id}")
    async def get_team(team_id: str):
        team = get_team_by_id(team_id, teams)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")

        return TeamGetResponse.from_team(team, async_mode=True)

    @playground_router.post("/teams/{team_id}/runs")
    async def create_team_run(
        team_id: str,
        message: str = Form(...),
        stream: bool = Form(True),
        monitor: bool = Form(True),
        session_id: Optional[str] = Form(None),
        user_id: Optional[str] = Form(None),
        files: Optional[List[UploadFile]] = File(None),
    ):
        logger.debug(f"Creating team run: {message} {session_id} {monitor} {user_id} {team_id} {files}")
        team = get_team_by_id(team_id, teams)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")

        if session_id is not None and session_id != "":
            logger.debug(f"Continuing session: {session_id}")
        else:
            logger.debug("Creating new session")
            session_id = str(uuid4())

        if monitor:
            team.monitoring = True
        else:
            team.monitoring = False

        base64_images: List[Image] = []
        base64_audios: List[Audio] = []
        base64_videos: List[Video] = []
        document_files: List[FileMedia] = []

        if files:
            for file in files:
                if file.content_type in ["image/png", "image/jpeg", "image/jpg", "image/webp"]:
                    try:
                        base64_image = process_image(file)
                        base64_images.append(base64_image)
                    except Exception as e:
                        logger.error(f"Error processing image {file.filename}: {e}")
                        continue
                elif file.content_type in ["audio/wav", "audio/mp3", "audio/mpeg"]:
                    try:
                        base64_audio = process_audio(file)
                        base64_audios.append(base64_audio)
                    except Exception as e:
                        logger.error(f"Error processing audio {file.filename}: {e}")
                        continue
                elif file.content_type in [
                    "video/x-flv",
                    "video/quicktime",
                    "video/mpeg",
                    "video/mpegs",
                    "video/mpgs",
                    "video/mpg",
                    "video/mpg",
                    "video/mp4",
                    "video/webm",
                    "video/wmv",
                    "video/3gpp",
                ]:
                    try:
                        base64_video = process_video(file)
                        base64_videos.append(base64_video)
                    except Exception as e:
                        logger.error(f"Error processing video {file.filename}: {e}")
                        continue
                elif file.content_type in [
                    "application/pdf",
                    "text/csv",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "text/plain",
                    "application/json",
                ]:
                    document_file = process_document(file)
                    if document_file is not None:
                        document_files.append(document_file)
                else:
                    raise HTTPException(status_code=400, detail="Unsupported file type")

        if stream:
            return StreamingResponse(
                team_chat_response_streamer(
                    team,
                    message,
                    session_id=session_id,
                    user_id=user_id,
                    images=base64_images if base64_images else None,
                    audio=base64_audios if base64_audios else None,
                    videos=base64_videos if base64_videos else None,
                    files=document_files if document_files else None,
                ),
                media_type="text/event-stream",
            )
        else:
            run_response = await team.arun(
                message=message,
                session_id=session_id,
                user_id=user_id,
                images=base64_images if base64_images else None,
                audio=base64_audios if base64_audios else None,
                videos=base64_videos if base64_videos else None,
                files=document_files if document_files else None,
                stream=False,
            )
            return run_response.to_dict()

    @playground_router.get("/teams/{team_id}/sessions", response_model=List[TeamSessionResponse])
    async def get_all_team_sessions(team_id: str, user_id: Optional[str] = Query(None, min_length=1)):
        team = get_team_by_id(team_id, teams)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")

        if team.storage is None:
            raise HTTPException(status_code=404, detail="Team does not have storage enabled")

        try:
            all_team_sessions: List[TeamSession] = team.storage.get_all_sessions(user_id=user_id, entity_id=team_id)  # type: ignore
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error retrieving sessions: {str(e)}")

        team_sessions: List[TeamSessionResponse] = []
        for session in all_team_sessions:
            title = get_session_title_from_team_session(session)
            team_sessions.append(
                TeamSessionResponse(
                    title=title,
                    session_id=session.session_id,
                    session_name=session.session_data.get("session_name") if session.session_data else None,
                    created_at=session.created_at,
                )
            )
        return team_sessions

    @playground_router.get("/teams/{team_id}/sessions/{session_id}")
    async def get_team_session(team_id: str, session_id: str, user_id: Optional[str] = Query(None, min_length=1)):
        team = get_team_by_id(team_id, teams)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")

        if team.storage is None:
            raise HTTPException(status_code=404, detail="Team does not have storage enabled")

        try:
            team_session: Optional[TeamSession] = team.storage.read(session_id, user_id)  # type: ignore
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error retrieving session: {str(e)}")

        if not team_session:
            raise HTTPException(status_code=404, detail="Session not found")

        team_session_dict = team_session.to_dict()
        if team_session.memory is not None:
            runs = team_session.memory.get("runs")
            if runs is not None:
                first_run = runs[0]
                # This is how we know it is a RunResponse or RunPaused
                if "content" in first_run or first_run.get("is_paused", False) or first_run.get("event") == "RunPaused":
                    team_session_dict["runs"] = []
                    for run in runs:
                        # We skip runs that are not from the parent team
                        if run.get("team_session_id") is not None and run.get("team_session_id") == session_id:
                            continue

                        first_user_message = None
                        for msg in run.get("messages", []):
                            if msg.get("role") == "user" and msg.get("from_history", False) is False:
                                first_user_message = msg
                                break
                        # Remove the memory from the response
                        team_session_dict.pop("memory", None)
                        team_session_dict["runs"].append(
                            {
                                "message": first_user_message,
                                "response": run,
                            }
                        )

        return team_session_dict

    @playground_router.post("/teams/{team_id}/sessions/{session_id}/rename")
    async def rename_team_session(team_id: str, session_id: str, body: TeamRenameRequest):
        team = get_team_by_id(team_id, teams)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")

        if team.storage is None:
            raise HTTPException(status_code=404, detail="Team does not have storage enabled")

        all_team_sessions: List[TeamSession] = team.storage.get_all_sessions(user_id=body.user_id, entity_id=team_id)  # type: ignore
        for session in all_team_sessions:
            if session.session_id == session_id:
                team.rename_session(body.name, session_id=session_id)
                return JSONResponse(content={"message": f"successfully renamed team session {body.name}"})

        raise HTTPException(status_code=404, detail="Session not found")

    @playground_router.delete("/teams/{team_id}/sessions/{session_id}")
    async def delete_team_session(team_id: str, session_id: str, user_id: Optional[str] = Query(None, min_length=1)):
        team = get_team_by_id(team_id, teams)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found")

        if team.storage is None:
            raise HTTPException(status_code=404, detail="Team does not have storage enabled")

        all_team_sessions: List[TeamSession] = team.storage.get_all_sessions(user_id=user_id, entity_id=team_id)  # type: ignore
        for session in all_team_sessions:
            if session.session_id == session_id:
                team.delete_session(session_id)
                return JSONResponse(content={"message": f"successfully deleted team session {session_id}"})

        raise HTTPException(status_code=404, detail="Session not found")

    @playground_router.get("/team/{team_id}/memories")
    async def get_team_memories(team_id: str, user_id: str = Query(..., min_length=1)):
        team = get_team_by_id(team_id, teams)
        if team is None:
            return JSONResponse(status_code=404, content="Teem not found.")

        if team.memory is None:
            return JSONResponse(status_code=404, content="Team does not have memory enabled.")

        if isinstance(team.memory, Memory):
            memories = team.memory.get_user_memories(user_id=user_id)
            return [
                MemoryResponse(memory=memory.memory, topics=memory.topics, last_updated=memory.last_updated)
                for memory in memories
            ]
        else:
            return []

    return playground_router
