from agno.agent import Agent, RunResponse  # noqa
from agno.models.langdb import LangDB

agent = Agent(
    model=LangDB(id="deepseek-chat", project_id="langdb-project-id"), markdown=True
)

# Get the response in a variable
# run: RunResponse = agent.run("Share a 2 sentence horror story")
# print(run.content)

# Print the response in the terminal
agent.print_response("Share a 2 sentence horror story")
