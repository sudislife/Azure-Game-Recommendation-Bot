# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
import difflib
import os
import sys
import json
import pandas as pd
import traceback
import urllib
import shutil
import tensorflow as tf

from tensorflow.keras.models import load_model
from datetime import datetime
from http import HTTPStatus
from aiohttp import web
from aiohttp.web import Request, Response, json_response
from botbuilder.core import (
    BotFrameworkAdapterSettings,
    TurnContext,
    BotFrameworkAdapter,
)
from botbuilder.core.integration import aiohttp_error_middleware
from botbuilder.schema import Activity, ActivityTypes
from config import DefaultConfig
from botbuilder.core import ActivityHandler, MessageFactory, TurnContext
from botbuilder.schema import ChannelAccount
from azure.ai.language.conversations import ConversationAnalysisClient
from azure.ai.language.conversations.authoring import ConversationAuthoringClient
from azure.core.credentials import AzureKeyCredential

with open("keys.json") as key:
    keys = json.load(key)

subscription_key = keys['key']
endpoint = "https://language7.cognitiveservices.azure.com/"
project_name = "Game_CLU"
deployment_name = "Game_Deployment"

credential = AzureKeyCredential(subscription_key)
model = load_model("best_model.h5")


def analyze(text):
    # Create a client
    analysis_client = ConversationAnalysisClient(
        endpoint=endpoint, credential=credential
    )
    authoring_client = ConversationAuthoringClient(
        endpoint=endpoint, credential=credential
    )
    intent = None
    entity = None
    app_id = None

    # Test the client
    with analysis_client:
        query = text
        task = {
            "kind": "Conversation",
            "analysisInput": {
                "conversationItem": {
                    "participantId": "1",
                    "id": "1",
                    "modality": "text",
                    "language": "en",
                    "text": query,
                },
                "isLoggingEnabled": False,
            },
            "parameters": {
                "projectName": project_name,
                "deploymentName": deployment_name,
                "verbose": True,
            },
        }

        result = analysis_client.analyze_conversation(
            task=task, content_type="application/json"
        )

    intent = result["result"]["prediction"]["topIntent"]
    if len(result["result"]["prediction"]["entities"]) > 0:
        for entity in result["result"]["prediction"]["entities"]:
            if "extraInformation" in entity:
                for data in entity["extraInformation"]:
                    if data["extraInformationKind"] == "ListKey":
                        app_id = data["key"]
            entity = entity["text"]
            break

    print("Intent: ", intent, "Entity: ", entity)
    print("Intent type: ", type(intent), "Entity type: ", type(entity))
    return intent, entity, app_id


def return_similar_title(df, title):
    try:
        matches = difflib.get_close_matches(title, df["title"], n=1, cutoff=0.4)
        return matches[0]
    except:
        return None


def _download_attachment_and_write(attachment) -> dict:
    """
    Retrieve the attachment via the attachment's contentUrl.
    :param attachment:
    :return: Dict: keys "filename", "local_path"
    """
    try:
        response = urllib.request.urlopen(attachment.content_url)
        headers = response.info()

        # If user uploads JSON file, this prevents it from being written as
        # "{"type":"Buffer","data":[123,13,10,32,32,34,108..."
        if headers["content-type"] == "application/json":
            data = bytes(json.load(response)["data"])
        else:
            data = response.read()

        # Clear out the Attachment folder
        shutil.rmtree("Attachment/test")
        os.mkdir("Attachment/test")

        local_filename = os.path.join("Attachment/test", attachment.name)
        with open(local_filename, "wb") as out_file:
            out_file.write(data)

        class_names = ["Call of Duty Black Ops iii", "Journey"]
        test_ds = tf.keras.preprocessing.image_dataset_from_directory(
            "Attachment",
            image_size=(150, 150),
        )
        entity = class_names[round(model.predict(test_ds)[0][0])]
        print("Entity: ", entity)
        print("Filename: ", attachment.name)
        print("Local path: ", local_filename)
        return entity

    except Exception as exception:
        print(exception)
        return {}


def findEntity(attachment):
    attachment_info = _download_attachment_and_write(attachment)

    return attachment_info


def recommend(entity, pred, app_id):
    if entity is None:
        return "Tell me a game you like and add the keyword 'recommend' to get a recommendation"

    first_part = ""
    entity = entity.lower()

    if entity not in pred["title"] and app_id is None:
        entity = return_similar_title(pred, entity)
        first_part = "I think you mean " + entity + ". "
        if entity is None:
            return "Sorry, I can't find that game"

    if app_id is not None:
        user = (
            pred[pred["app_id"] == int(app_id)]
            .sort_values(by="prediction", ascending=False)["user_id"]
            .values[0]
        )
    else:
        user = (
            pred[pred["title"] == entity]
            .sort_values(by="prediction", ascending=False)["user_id"]
            .values[0]
        )

    recommendations = (
        pred[pred["user_id"] == user]
        .sort_values(by="prediction", ascending=False)["title"]
        .values[:5]
    )

    response = (
        first_part
        + "Here are some of the games I recommend you try: "
        + ", ".join(recommendations)
    )
    return response


class Bot(ActivityHandler):
    async def on_members_added_activity(
        self, members_added: [ChannelAccount], turn_context: TurnContext
    ):
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity("Hello and welcome!")

    async def on_message_activity(self, turn_context: TurnContext):
        intent = None
        entity = None
        app_id = None
        pred = pd.read_csv("predictions.csv")
        # Checks if the message is an attachment
        if turn_context.activity.attachments is not None:
            entity = findEntity(turn_context.activity.attachments[0])
            if entity is not None:
                intent = "Recommend"

        # If not, analyzes the text
        else:
            text = turn_context.activity.text

            intent, entity, app_id = analyze(text)

        # Responds based on the intent
        if intent == "Salutation":
            response = "Hello!"

        elif intent == "Recommend":
            response = recommend(entity, pred, app_id)

        elif intent == "BuyGame":
            if entity is not None:
                response = "Adding " + entity + " to your cart."
            else:
                response = "Sorry, I can't find that game"

        else:
            response = "Sorry, I don't understand."

        # Returns the response
        return await turn_context.send_activity(MessageFactory.text(response))


CONFIG = DefaultConfig()

# Create adapter.
# See https://aka.ms/about-bot-adapter to learn more about how bots work.
SETTINGS = BotFrameworkAdapterSettings(CONFIG.APP_ID, CONFIG.APP_PASSWORD)
ADAPTER = BotFrameworkAdapter(SETTINGS)


# Catch-all for errors.
async def on_error(context: TurnContext, error: Exception):
    # This check writes out errors to console log .vs. app insights.
    # NOTE: In production environment, you should consider logging this to Azure
    #       application insights.
    print(f"\n [on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()

    # Send a message to the user
    await context.send_activity("The bot encountered an error or bug.")
    await context.send_activity(
        "To continue to run this bot, please fix the bot source code."
    )
    # Send a trace activity if we're talking to the Bot Framework Emulator
    if context.activity.channel_id == "emulator":
        # Create a trace activity that contains the error object
        trace_activity = Activity(
            label="TurnError",
            name="on_turn_error Trace",
            timestamp=datetime.utcnow(),
            type=ActivityTypes.trace,
            value=f"{error}",
            value_type="https://www.botframework.com/schemas/error",
        )
        # Send a trace activity, which will be displayed in Bot Framework Emulator
        await context.send_activity(trace_activity)


ADAPTER.on_turn_error = on_error

# Create the Bot
BOT = Bot()


# Listen for incoming requests on /api/messages
async def messages(req: Request) -> Response:
    # Main bot message handler.
    if "application/json" in req.headers["Content-Type"]:
        body = await req.json()
    else:
        return Response(status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

    activity = Activity().deserialize(body)
    auth_header = req.headers["Authorization"] if "Authorization" in req.headers else ""

    response = await ADAPTER.process_activity(activity, auth_header, BOT.on_turn)
    if response:
        return json_response(data=response.body, status=response.status)
    return Response(status=HTTPStatus.OK)


APP = web.Application(middlewares=[aiohttp_error_middleware])
APP.router.add_post("/api/messages", messages)

if __name__ == "__main__":
    try:
        web.run_app(APP, host="localhost", port=CONFIG.PORT)
    except Exception as error:
        raise error
