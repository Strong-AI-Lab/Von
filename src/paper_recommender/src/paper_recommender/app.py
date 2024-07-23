"""Entry point for the paper_recommender Slack app.

Handles all the events and actions that the app is subscribed to.
"""

import os
import logging
import datetime
import configparser
from pathlib import Path
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi

# Use the slack_bolt package to create the app
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# from slack_bolt.async_app import AsyncApp
# from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .paper_extraction.from_url import extract_abstract_from_url, known_domain
from .engine.open_ai import paper_recommendation
from .mangodb import crud as db_crud
from .slack_templates import home, modal, message as message_block


### Load the configurations from the configs.ini file ###
project_dir = Path(__file__).parent.parent.parent
config_path = (project_dir / "configs/configs.ini").resolve()
configs = configparser.ConfigParser(allow_no_value=True)
configs.read(config_path)
if not configs["App"]["app_log"]:
    Path(str((project_dir / "logs").resolve())).mkdir(parents=True, exist_ok=True)
    configs["App"]["app_log"] = str((project_dir / "logs/app.log").resolve())
if not configs["App"]["unknown_domains"]:
    configs["App"]["unknown_domains"] = str((project_dir / "logs/unknown_domains.txt").resolve())

### Initialise the app and database ###
# create a MongoClient instance
# Set the Stable API version when creating a new client
mongo_client = MongoClient(configs["App"]["mongodb_connect_str"], server_api=ServerApi("1"))  # type: MongoClient
# connect to the paper_recommender database
# A new database with the specified name will be created if it doesn't exist, when inserting a document
db = mongo_client["paper_recommender"]

# Initialize the app with the bot token and signing secret
# Signing secret token is only used when the standard http mode is used,
# instead of Socket model.
# These tokens are stored in the environment variables, and will be searched
# automatically even if they are not explicitly provided here.
app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),  # not used since we are using Socket Mode
)
# app = AsyncApp(
#     token=os.environ.get("SLACK_BOT_TOKEN"),
#     signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
# )


# open app home view
@app.event("app_home_opened")
def home_tab(client, event, logger):
    """Displays the app's app home when the user opens the app's app home.

    Shows the projects of the user if any exists.
    """
    try:
        # the user that opened the app's app home
        user_id = event["user"]
        # get projects of the user
        projects = db_crud.get_projects_for_user(db, user_id)

        # views.publish is the method that the app uses to push a view to the Home tab
        client.views_publish(user_id=user_id, view=home.home_view(projects, db_crud.user_exists(db, user_id)))
    except Exception:
        logger.exception("Error publishing home tab.")


# open a modal view for the user to enter the project description
@app.action("add_project")
def open_home_modal(ack, body, client):
    """Opens a modal view for the user to enter or update the project description."""
    # Acknowledge the command request
    ack()
    # Call views_open with the built-in client
    client.views_open(
        # Pass a valid trigger_id within 3 seconds of receiving it
        trigger_id=body["trigger_id"],
        # View payload
        view=modal.project_modal(),
    )


# handles requests to delete a project for user
@app.action("delete_project")
def delete_user_project(ack, body, say, client, logger):
    """Delete a project for user."""
    ack()
    user_id = body["user"]["id"]
    if not db_crud.delete_project(db, user_id, body["actions"][0]["block_id"]):
        say(channel=user_id, text="Error deleting the project... Please contact the APP developer.")
    else:
        # update the home tab with the project description
        try:
            projects = db_crud.get_projects_for_user(db, user_id)
            # views.publish is the method that the app uses to push a view to the Home tab
            client.views_publish(user_id=user_id, view=home.home_view(projects, True))
        except Exception:
            logger.exception("Error updating home tab with new project.")


# Handle a modal view_submission request for creating a new project
# refer to the correct callback_id of a modal view
@app.view("project_modal")
def handle_home_submission(ack, body, client, view, logger):
    """Handles the request for creating a new project.

    Add a new project for user in the database and updates the home tab with the new project information.
    If the user does not exist, a new user is created first, then add the new project.
    """
    project_description = view["state"]["values"]["input_description"]["ml_input"]["value"]
    project_title = view["state"]["values"]["input_title"]["sl_input"]["value"]
    user_id = body["user"]["id"]
    # Validate the inputs
    errors = {}
    if project_description is None or len(project_description) < 10:
        errors["input_description"] = "The project description seems too short. Was there a mistake?"
        ack(response_action="errors", errors=errors)
        return
    if project_title is None or len(project_title) < 1:
        errors["input_title"] = "The project title seems too short. Was there a mistake?"
        ack(response_action="errors", errors=errors)
        return

    if db_crud.user_exists(db, user_id):
        if not db_crud.add_project(db, user_id, project_title, project_description):
            errors["input_description"] = "Error updating the project description... Please contact the APP developer."
            ack(response_action="errors", errors=errors)
            return
    else:
        if not db_crud.create_user(db, user_id):
            errors["input_description"] = "Error creating a new user... Please contact the APP developer."
            ack(response_action="errors", errors=errors)
            return
        if not db_crud.add_project(db, user_id, project_title, project_description):
            errors["input_description"] = "Error updating the project description... Please contact the APP developer."
            ack(response_action="errors", errors=errors)
            return

    # Everything is ok. Acknowledge the view_submission request and close the modal
    ack()

    # update the home tab with the new project
    try:
        projects = db_crud.get_projects_for_user(db, user_id)
        client.views_publish(user_id=user_id, view=home.home_view(projects, True))
    except Exception:
        logger.exception("Error updating home tab with new project.")


# This will match any message posted in the subscribed channels that contains a URL
@app.message(
    r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+",
    matchers=[lambda event: event["channel_type"] == "channel"],
)
def extract_urls(say, message, logger):
    """Handles the message event whenver someone posts a message with a URL in the subscribed channel.

    It extracts the URL from the message and retrieves paper abstract. A recommendation engine is then used to
    recommend the paper to users based on their project descriptions, along with explanations. The recommmendation
    is sent through Slack private channels to the users.

    By default, a message is only sent to a user if the paper is recommended. However, if the user is a VIP member,
    they will receive messages regardless of the recommendation decision. All messages will include buttons for the
    user to provide feedback on the recommendation.

    By adding "#dev" at the beginning of the message, the message will only be sent to the message sender. This is a
    feature for development and testing purposes, to avoid spaming users.

    Both papers and recommendations are recorded in the database. If the URL contains unknown domains it will
    be ignored, however, the domain and the URL are logged for further investigation and future decision of
    adding new sources for papers.
    """
    # extract urls from the message
    urls = []
    for block in [block for block in message["blocks"] if "elements" in block]:
        for element in [element for element in block["elements"] if "elements" in element]:
            for el in [el for el in element["elements"] if el["type"] == "link"]:
                url = el["url"]
                if known_domain(url, configs["App"]["unknown_domains"]):
                    urls.append(url)

    try:
        # if the message starts with "#dev", only send the message to the message sender
        if message["text"].startswith("#dev"):
            users_curser = db.users.find(
                {
                    "_id": message["user"],
                },
                projection={
                    "_id": 1,
                    "projects": 1,
                    "vip": 1,
                },
            )
        else:
            # get all users
            users_curser = db.users.find(
                projection={
                    "_id": 1,
                    "projects": 1,
                    "vip": 1,
                }
            )
        # iterate over the curser object of all users to get the user id and project description
        # NOTE: curser object is not list; it can only be iterated once and indexing would be very inefficient
        for user in users_curser:
            for project in user["projects"]:
                user_id, project_description, project_id, vip_status = (
                    user["_id"],
                    project["description"],
                    project["project_id"],
                    user["vip"],
                )

                for url in urls:
                    # extract paper info from the url
                    paper = extract_abstract_from_url(url)
                    if paper:
                        # record the paper in the database
                        paper_id = db_crud.insert_paper(db, paper)
                        # if not paper_id it means paper insertion or retrieval failed
                        # in that case, skip the recommendation process
                        if not paper_id:
                            continue

                        recommendation = paper_recommendation(project_description, paper.abstract, configs)
                        if recommendation is None:
                            continue
                        decision, explanation = recommendation.decision, recommendation.explanation
                        # record recommendation history regardless of the decision
                        record = {
                            "user_id": user_id,
                            "project_id": project_id,
                            "paper_id": paper_id,
                            "project_description": project_description,
                            "paper_abstract": paper.abstract,
                            "paper_url": url,
                            "recommend": decision,
                            "explanation": explanation,
                            "created_at": datetime.datetime.now(tz=datetime.timezone.utc),
                        }
                        recommendation_id = db_crud.insert_recommendation(db, record)
                        # if recommendation insertion failed, skip the message posting
                        if not recommendation_id:
                            continue

                        # send a private message to the user if it is a recommendation
                        if decision:
                            say(
                                channel=user_id,
                                blocks=message_block.positive_recommendation_block(user_id, url, explanation),
                                text=(
                                    f"Hi there, <@{user_id}>! Here is a paper you might be "
                                    f"interested in: {url}\n{explanation}"
                                ),  # fallback text
                                metadata={
                                    "event_type": "recommendation_created",
                                    "event_payload": {"recommendation_id": str(recommendation_id)},
                                },
                            )
                        else:
                            if vip_status:
                                say(
                                    channel=user_id,
                                    blocks=message_block.negative_recommendation_block(user_id, url, explanation),
                                    text=(
                                        f"Hi there, <@{user_id}>! I don't think this paper is relevant to "
                                        f"your project, but you might disagree: {url}\n{explanation}"
                                    ),  # fallback text
                                    metadata={
                                        "event_type": "recommendation_created",
                                        "event_payload": {"recommendation_id": str(recommendation_id)},
                                    },
                                )
    except Exception:
        logger.exception("Failed to post the recommendation message.")


# handle message events in direct messages
@app.event(
    "message",
    matchers=[lambda event: event["channel_type"] == "im"],
)
def handle_message_im_events(say, event):
    """Search for papers whose titles, autor lists or abstracts that contain the provided keywords."""
    search_string = event["text"]
    papers = db_crud.keyword_search_papers(db, search_string)
    if papers:
        counter = 0
        for paper in papers:
            say(
                text=paper["url"],
                channel=event["user"],
            )
            counter += 1
        if counter == 0:
            say(
                text="No papers found with the provided keywords.",
                channel=event["user"],
            )
    else:
        say(
            text="Something went wrong while searching for papers. The error has been logged and will be investigated.",
            channel=event["user"],
        )


##### Replace this with the new event handler or remove file_shared event type #####
@app.event("file_shared")
def handle_file_shared_events(body, logger):
    """Logs file_shared events from the subscribed channel."""
    logger.info(body)


# Handles papers shared in PDF files
# @app.event("file_shared")
# def extract_pdfs(say, client, event, logger):
#     """Handles the event whenever someone shares a pdf file in subscribed channels.

#     It extracts paper information from the shared PDF file, including the abstract. A recommendation engine is
#     then used to recommend the paper to users based on their project descriptions, along with explanations.
#     The recommmendation is sent through Slack private channels to the users. Both papers and recommendations are
#     recorded in the database.

#     By default, a message is only sent to users if the paper is recommended. However, if the user is a VIP member,
#     they will receive messages regardless of the recommendation decision. All messages will include buttons for the
#     user to provide feedback on the recommendation.

#     By adding "#dev" at the beginning of the message, the message will only be sent to the message sender. This is a
#     feature for development and testing purposes, to avoid spaming users.
#     """
#     try:
#         file_id = event["file_id"]
#         file = client.files_info(file=file_id)
#         if not file["ok"]:
#             raise ValueError("Failed to retrieve the file information.")
#         file = file["file"]
#         if file["filetype"] != "pdf":
#             return
#         url = file["url_private"]
#         url_download = file["url_private_download"]

#     except Exception:
#         logger.exception("Failed to post the recommendation message.")


# respond to joining Lab-Rats VIP Club!
@app.action("join_club")
def join_club(ack, body, say):
    """Handles request to join the VIP club.

    Updates the database with the user's VIP status and sends a message to the user to confirm their VIP status.
    The VIP users will receive recommendation messges regardless of the recommendation decision, and
    will gain early access to new features.
    """
    ack()
    # update the database with the user's VIP status
    if not db_crud.update_user_vip_status(db, body["user"]["id"], True):
        say(channel=body["user"]["id"], text="Error updating the VIP status... Please contact the APP developer.")

    else:
        say(
            channel=body["user"]["id"],
            text=(
                "You have been registered as a Lab-Rats VIP member! :tada: You will now receive all papers shared "
                "by lab members regardless of my recommendation decisions, along with explanations. Additionally, "
                "you will gain early access to new and more advanced features as I am upgraded over time."
            ),
        )


# # respond to leaving Lab-Rats VIP Club!
@app.action("leave_club")
def leave_club(ack, body, say):
    """Handles request to leave the VIP club.

    Updates the database with the user's VIP status and sends a message to the user to confirm their VIP status."""
    ack()
    # update the database with the user's VIP status
    if not db_crud.update_user_vip_status(db, body["user"]["id"], False):
        say(channel=body["user"]["id"], text="Error updating the VIP status... Please contact the APP developer.")

    else:
        say(
            channel=body["user"]["id"],
            text="You have successfully left the Lab-Rats VIP Club. You are always welcome to join again.",
        )


# respond to positive feedback from user
@app.action("feedback_positive")
def positive_feedback(ack, body):
    """Handles the user's positive feedback on the recommendation.

    Updates the associated recommendation in the database with the user's feedback.
    """
    ack()
    recommendation_id = body["message"]["metadata"]["event_payload"]["recommendation_id"]
    # update the database with the user's feedback
    db_crud.update_recommendation_feedback(db, recommendation_id, True)


# respond to negative feedback from user
@app.action("feedback_negative")
def negative_feedback(ack, body):
    """Handles the user's negative feedback on the recommendation.

    Updates the associated recommendation in the database with the user's feedback.
    """
    ack()
    recommendation_id = body["message"]["metadata"]["event_payload"]["recommendation_id"]

    # update the database with the user's feedback
    db_crud.update_recommendation_feedback(db, recommendation_id, False)


# open a modal view for the user to enter feedback explanation
@app.action("feedback_explanation")
def open_feedback_modal(ack, body, client):
    """Handles the user's negative feedback on the recommendation.

    Creates a modal view for the user to enter their explanation for the negative feedback.
    """
    ack()
    recommendation_id = body["message"]["metadata"]["event_payload"]["recommendation_id"]

    # open modal view for the user to enter feedback explanation
    client.views_open(
        # Pass a valid trigger_id within 3 seconds of receiving it
        trigger_id=body["trigger_id"],
        # View payload
        view=modal.feedback_modal(recommendation_id),
    )


# Handle a modal view_submission request for user's explanation of their negative feedback
@app.view("feedback_modal")
def handle_feedback_submission(ack, view):
    """Handles the submission of user's explanation of their feedback on the recommendation.

    Updates the associated recommendation in the database with the user's feedback.
    """
    user_explanation = view["state"]["values"]["user_input_feedback"]["user_feedback_input"]["value"]
    # Validate the inputs
    errors = {}
    if user_explanation is None or len(user_explanation) < 5:
        errors["user_input_feedback"] = "The explanation seems too short. Was there a mistake?"
        ack(response_action="errors", errors=errors)
        return

    recommendation_id = view["private_metadata"]
    # update the database with the user's feedback
    if not db_crud.update_recommendation_feedback_reason(db, recommendation_id, user_explanation):
        errors["user_input_feedback"] = "Error adding the feedback to the database... Please contact the APP developer."
        ack(response_action="errors", errors=errors)
    else:
        ack()


@app.event("message")
def handle_message_events(body, logger):
    """Logs other message events from the subscribed channel."""
    logger.info(body)


# async def start_app():
#     handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
#     await handler.start_async()


def start_app():
    """Entry point to start the Slack app. Once the package has been installed using pip,
    the app can be started by simply running `paper_recommender` in the terminal.

    This implementation uses the Socket Mode adapter to handle incoming events from Slack.
    It avoids having to expose a static public endpoint that Slack can use to send the app events,
    when uisng standard HTTP RESTful Web API. RESTful Web API relies on the request-response model of
    communication between clients and servers; the connection is terminated by the server when the response is served.
    If the app is deployed behind a corporate firewall, Slack cannot initialise a connection again for any event or
    action request. Websocket is a protocol that provides constant full-duplex communication channels over a single
    TCP connection; after the server that hosts the app initialises the connection with Slack, the connection is kept
    open and Slack can send events and actions to the app at any time. The app can also send events and actions to
    Slack at any time.

    NOTE: The app requires several environment variables to be set. Add the following to ~/.bash_profile
    (Interactive login shell) or ~/.bashrc (Interactive non-login shell). Then run `source ~/.bash_profile` or
    `source ~/.bashrc` to apply the changes.
    - export SLACK_BOT_TOKEN=***      # The bot token is used to authenticate the app with Slack,
    regardless of the mode used.
    - export SLACK_APP_TOKEN=***      # Only used in Socket Mode. Not used in HTTP Web API mode.
    - export SLACK_SIGNING_SECRET=*** # Only used in standard HTTP Web API mode. Not used in Socket mode.
    - export OPENAI_API_KEY=***       # The API key for OpenAI API. Only required if OpenAI API is used.

    Other configurations are set in the /configs/configs.ini file.
    The required one is the connection string to the MongoDB database.
    """
    # set up logging level and location
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%d/%m/%Y %H:%M:%S %z",
        filename=configs["App"]["app_log"],
    )

    # The HTTP server is using a built-in development adapter, which is responsible for
    # handling and parsing incoming events from Slack.
    # The HTTP server adapter is not recommended for production. Bolt for Python includes a collection of
    # built-in adapters that can be imported and used with your app. You can explore adapters and
    # sample usage in the repository's examples folder.
    # To develop locally use ngrok, which allows you to expose a public endpoint that
    # Slack can use to send your app events
    # Use port 3000 because ngrok was started with port 3000
    # app.start(port=int(os.environ.get("PORT", 3000)))

    # using Socket Mode instead of creating a server with endpoints that Slack sends payloads to
    # Socket model doesn’t require public static IP, and can operate behind the firewall
    # But the app cannot be distributed
    handler = SocketModeHandler(
        app, os.environ["SLACK_APP_TOKEN"]
    )  # SLACK_APP_TOKEN must be used with Socket Mode. Will be automatically searched if not provided.
    handler.start()

    # # async app
    # import asyncio
    # asyncio.run(main())


if __name__ == "__main__":
    start_app()
