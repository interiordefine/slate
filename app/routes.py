import os
import json
from datetime import datetime

from flask import request, make_response, jsonify, redirect, url_for
from flask import current_app as app
from slack_sdk.errors import SlackApiError
from sqlalchemy import and_

import app.utils as utils
from app import client, signature_verifier
from app.models import Submission, Standup, User, Team, db
from app.utils import authenticate
from app.constants import (
    ALL,
    ACTIVE,
    INACTIVE,
    NO_USER_ERROR_MESSAGE,
    POST_PUBLISH_STATS,
    NO_USER_SUBMIT_MESSAGE,
    BUTTON_TRIGGER,
    SLASH_COMMAND_TRIGGER,
    BLOCK_SIZE,
    SUBMISSION_UPDATED_MESSAGE,
    STANDUP_INFO_SECTION
)


# Callback for entrypoint trigger on Slack (slash command etc.)
@app.route("/slack/standup-trigger/", methods=["POST", "GET"])
def standup_trigger():
    if request.method == "GET":
        data = json.loads(request.args["messages"])
        user_id = data.get("user", {}).get("id")
        action_type = BUTTON_TRIGGER
    else:
        if not signature_verifier.is_valid_request(request.get_data(), request.headers):
            return make_response("invalid request", 403)

        data = request.form
        user_id = data.get("user_id")
        action_type = SLASH_COMMAND_TRIGGER

    try:
        user = User.query.filter_by(user_id=user_id).first()
        if action_type == BUTTON_TRIGGER:
            team = (
                db.session.query(Team)
                .join(User.team)
                .filter(User.id == user.id)
                .first()
            )
        else:
            team_name = data.get("text")
            if not team_name:
                return make_response(
                    f"Slash command format is `/standup <team-name>`.\nYour commands: {', '.join(utils.get_user_slash_commands(user))}",
                    200,
                )
            team = Team.query.filter_by(name=team_name).first()

        # TODO: Check if this user it allowed in this team's standup especially
        # in the case of slash command trigger.
        standup = team.standup

        if submission := utils.submission_exists(user, standup):
            client.views_open(
                trigger_id=data.get("trigger_id"),
                view=utils.create_edit_view(standup, submission)
            )

        client.views_open(
            trigger_id=data.get("trigger_id"),
            view=utils.get_standup_view(standup)
        )
        return make_response("", 200)
    except SlackApiError as e:
        code = e.response["error"]
        return make_response(f"Failed to open a modal due to {code}", 200)
    except AttributeError:
        return make_response(
            f"No user details or standup exists for this request.\n{NO_USER_ERROR_MESSAGE}",
            200,
        )

    return make_response("invalid request", 403)


# Callback for form submission on Slack
@app.route("/slack/submit_standup/", methods=["POST"])
def standup_modal():
    if not signature_verifier.is_valid_request(request.get_data(), request.headers):
        return make_response("invalid request", 403)

    payload = json.loads(request.form.get("payload"))
    standup_submission = json.dumps(payload.get("view"))

    # Triggered by action button click. Redirect to open standup form.
    if payload.get("type") == "block_actions":
        return redirect(
            url_for("standup_trigger", messages=request.form.get("payload"))
        )

    if payload and utils.is_submission_eligible(payload):
        user_payload = payload.get("user", {})
        callback_id = payload.get("view", {}).get("callback_id", "")

        user = User.query.filter_by(user_id=user_payload.get("id")).first()
        standup = Standup.query.filter(Standup.trigger == callback_id).first()

        todays_datetime = datetime(
            datetime.today().year, datetime.today().month, datetime.today().day
        )

        is_edit = False
        if submission := utils.submission_exists(user, standup):
            client.chat_postMessage(channel=user.user_id,
                                    text=SUBMISSION_UPDATED_MESSAGE)
            submission.standup_submission = standup_submission
            is_edit = True
        else:
            submission = Submission(user_id=user.id,
                                    standup_submission=standup_submission,
                                    standup_id=standup.id,
                                    standup=standup)

        db.session.add(submission)
        db.session.commit()

    utils.after_submission(submission, is_edit)

    return make_response("", 200)


# Request to publish standup to a Slack channel
@app.route("/slack/publish_standup/<team_name>/", methods=["GET"])
@authenticate
def publish_standup(team_name):

    try:
        todays_datetime = datetime(
            datetime.today().year, datetime.today().month, datetime.today().day
        )

        team = Team.query.filter_by(name=team_name).first()
        if not team:
            return make_response(f'Team "{team_name}" does not exist', 404)

        # Get all active users for this team
        users = (
            db.session.query(User)
            .join(Team.user)
            .filter(Team.id == team.id, User.is_active)
        )

        standup = Standup.query.filter(Standup.trigger == team_name).first()
        submissions = Submission.query.filter(
            and_(
                Submission.created_at >= todays_datetime,
                Submission.user_id.in_([user.id for user in users]),
                Submission.standup == standup,
            )
        )

        message_response = client.chat_postMessage(
            channel=team.standup.publish_channel,
            text="Standup complete",
            blocks=[STANDUP_INFO_SECTION],
        )

        blocks_chunk = utils.chunk_blocks(utils.build_standup(submissions, True),
                                          BLOCK_SIZE)
        for blocks in blocks_chunk:
            client.chat_postMessage(
                channel=team.standup.publish_channel,
                text="Standup complete",
                thread_ts=message_response.get("ts"),
                blocks=blocks,
            )
        if POST_PUBLISH_STATS:
            no_submit_users = utils.post_publish_stat(users)
            message = f"{NO_USER_SUBMIT_MESSAGE} {', '.join(no_submit_users)}"

            client.chat_postMessage(
                channel=team.standup.publish_channel, text=message)

        return make_response(json.dumps(utils.build_standup(submissions)), 200)
    except SlackApiError as e:
        code = e.response["error"]
        return make_response(f"Failed due to {code}", 200)


# APIs start here

# Add user to DB
@app.route("/api/add_user/", methods=["POST"])
@authenticate
def add_user():
    payload = request.json
    if payload:
        team_id = payload.get("team_id")
        team = Team.query.filter(Team.id == team_id).first()

        user = User(
            user_id=payload.get("user_id"),
            username=payload.get("username"),
            is_active=payload.get("is_active"),
            team=[team],
        )

        db.session.add(user)
        db.session.commit()
        return jsonify({"sucess": True, "id": user.id})
    return jsonify({"sucess": False})


# Update user
@app.route("/api/update_user/<user_id>/", methods=["PUT"])
@authenticate
def update_user(user_id):
    payload = request.json
    if payload:
        user = User.query.get(user_id)
        team_id = payload.get("team_id")
        team = Team.query.filter(Team.id == team_id).first()

        user.user_id = payload.get("user_id")
        user.username = payload.get("username")
        user.is_active = payload.get("is_active")
        user.team.append(team)

        db.session.add(user)
        db.session.commit()

        return jsonify({"sucess": True})
    return jsonify({"sucess": False})


# Get user by username
@app.route("/api/get_user/<username>/", methods=["GET"])
@authenticate
def get_user(username):
    users = User.query.filter(User.username.contains(username)).all()
    return jsonify({"success": True, "users": utils.prepare_user_response(users)})


# Get all users
@app.route("/api/get_users/", methods=["GET"])
@authenticate
def get_users():
    users = User.query.all()
    return jsonify({"success": True, "users": utils.prepare_user_response(users)})


# Add a new standup to DB
@app.route("/api/add_standup/", methods=["POST"])
@authenticate
def add_standup():
    payload = request.json
    if utils.is_standup_valid(**payload):
        payload["standup_blocks"] = utils.questions_to_blockkit(
            payload.get("questions")
        )
        data = utils.prepare_standup_table_data(**payload)

        try:
            standup = Standup(**data)
            team_id = payload.get("team_id")
            team = Team.query.filter(Team.id == team_id).first()
            standup.team = team

            db.session.add(standup)
            db.session.commit()

            return jsonify({"success": True, "standup_id": standup.id})
        except Exception:
            return jsonify(
                {
                    "success": False,
                    "reason": "Could not save the submitted standup to DB",
                }
            )
    return jsonify(
        {
            "success": False,
            "reason": "Incorrect payload. Required: questions, is_active, trigger, publish_channel",
        }
    )


# Update an existing standup
@app.route("/api/update_standup/<standup_id>/", methods=["PUT"])
@authenticate
def update_standup(standup_id):
    payload = request.json
    if utils.is_standup_valid(**payload):
        try:
            payload["standup_blocks"] = utils.questions_to_blockkit(
                payload.get("questions")
            )
            data = utils.prepare_standup_table_data(**payload)

            Standup.query.get(standup_id).update(**data)
            db.session.commit()
            return jsonify({"success": True})
        except Exception:
            return jsonify(
                {
                    "success": False,
                    "reason": "Could not save the updated standup to DB",
                }
            )
    return jsonify(
        {
            "success": False,
            "reason": "Incorrect payload. Required: questions, is_active, trigger, publish_channel",
        }
    )


@app.route("/api/get_standup/<standup_id>/", methods=["GET"])
@authenticate
def get_standup(standup_id):
    # remove all keys from dict starting with "_"
    filter_keys = lambda x: {k: v for k, v in x.items() if not k.startswith("_")}

    # If id in request args then return standup for id
    if standup_id.isnumeric():
        try:
            standup = Standup.query.filter_by(id=standup_id).first()
            return jsonify(
                {
                    "success": True,
                    "standup": utils.format_standup(filter_keys(standup.__dict__)),
                }
            )
        except Exception:
            return jsonify(
                {
                    "success": False,
                    "reason": f"Standup for id {standup_id} does not exist",
                }
            )
    return jsonify({"success": False, "reason": "Incorrect standup_id."})


# Fetch standups based on their status (active, inactive, all)
@app.route("/api/get_standups/", methods=["GET"])
@authenticate
def get_standups():
    status = request.args.get("status", ALL)

    # remove all keys from dict starting with "_"
    filter_keys = lambda x: {k: v for k, v in x.items() if not k.startswith("_")}

    if status == ACTIVE:
        standups = Standup.query.filter_by(is_active=True).all()
    elif status == INACTIVE:
        standups = Standup.query.filter_by(is_active=False).all()
    else:
        standups = Standup.query.all()

    filtered_standups = [
        utils.format_standup(filter_keys(standup.__dict__)) for standup in standups
    ]

    return jsonify({"success": True, "standups": filtered_standups})


# Delete a standup
@app.route("/api/delete_standup/<standup_id>/", methods=["DELETE"])
@authenticate
def delete_standup(standup_id):
    Standup.query.filter_by(id=standup_id).delete()
    db.session.commit()
    return jsonify({"success": True})


# Delete all previous submissions
@app.route("/api/delete_submissions/", methods=["DELETE"])
@authenticate
def delete_submissions():
    todays_datetime = datetime(
        datetime.today().year, datetime.today().month, datetime.today().day
    )
    Submission.query.filter(Submission.created_at < todays_datetime).delete()


# Notify users who have not submitted the standup yet
@app.route("/api/notify_users/<team_name>/", methods=["GET"])
@authenticate
def notify_users(team_name):
    team = Team.query.filter_by(name=team_name).first()

    # Get all active users for this team
    users = (
        db.session.query(User)
        .join(Team.user)
        .filter(Team.id == team.id, User.is_active)
    ).all()

    for user in users:
        num_teams = len(user.team)

        todays_datetime = datetime(
            datetime.today().year, datetime.today().month, datetime.today().day
        )

        submissions = user.submission.filter(
            Submission.created_at >= todays_datetime
        ).all()

        # TODO: This assumes submissions for all teams. It should check
        # submission for only this team (requested in the API request)
        # This will need to submission object to have reference to
        # the standup it's associated with.
        if len(submissions) < num_teams:
            text, blocks = utils.prepare_notification_message(user)
            client.chat_postMessage(
                channel=user.user_id, text=text, blocks=blocks)
    return jsonify({"success": True})


# Get submission for user id
@app.route("/api/get_submission/<user_id>/", methods=["GET"])
@authenticate
def get_submission(user_id):
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    try:
        if start_date:
            start_date = datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            end_date = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return jsonify(
            {
                "success": False,
                "reason": "Invalid date format. Use format yyyy-mm-dd",
            }
        )

    if start_date and end_date:
        submissions = Submission.query.filter(
            and_(
                Submission.user_id == user_id,
                Submission.created_at >= start_date,
                Submission.created_at <= end_date,
            )
            .order_by(Submission.created_at.desc())
            .all()
        )

    elif start_date:
        submissions = Submission.query.filter(
            and_(
                Submission.user_id == user_id,
                Submission.created_at >= start_date,
            )
            .order_by(Submission.created_at.desc())
            .all()
        )

    elif end_date:
        submissions = Submission.query.filter(
            and_(
                Submission.user_id == user_id,
                Submission.created_at <= end_date,
            )
            .order_by(Submission.created_at.desc())
            .all()
        )

    else:
        submissions = (
            Submission.query.filter_by(user_id=user_id)
            .order_by(Submission.created_at.desc())
            .limit(50)
            .all()
        )

    return jsonify(
        {
            "success": True,
            "submissions": [
                utils.prepare_user_submission(submission) for submission in submissions
            ],
        }
    )


# Get submissions
@app.route("/api/get_submissions/", methods=["GET"])
@authenticate
def get_submissions():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    try:
        if start_date:
            start_date = datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            end_date = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return jsonify(
            {
                "success": False,
                "reason": "Invalid date format. Use format yyyy-mm-dd",
            }
        )

    if start_date and end_date:
        submissions = (
            Submission.query.filter(
                Submission.created_at >= start_date,
                Submission.created_at <= end_date,
            )
            .order_by(Submission.created_at.desc())
            .all()
        )
    elif start_date:
        submissions = (
            Submission.query.filter(Submission.created_at >= start_date)
            .order_by(Submission.created_at.desc())
            .all()
        )
    elif end_date:
        submissions = (
            Submission.query.filter(Submission.created_at <= end_date)
            .order_by(Submission.created_at.desc())
            .all()
        )
    else:
        submissions = (
            Submission.query.order_by(
                Submission.created_at.desc()).limit(50).all()
        )

    return jsonify(
        {
            "success": True,
            "submissions": [
                utils.prepare_user_submission(submission) for submission in submissions
            ],
        }
    )


# Add a team to DB
@app.route("/api/add_team/", methods=["POST"])
@authenticate
def add_team():
    payload = request.json

    if payload:
        standup = Standup.query.filter(
            Standup.id == payload.get("standup_id")).first()

        team = Team(standup=standup, name=payload.get("name"))

        db.session.add(team)
        db.session.commit()

    return jsonify({"success": True, "team_id": team.id})


# Get all teams
@app.route("/api/get_teams/", methods=["GET"])
@authenticate
def fetch_teams():
    teams = Team.query.all()
    response = []
    for team in teams:
        team_data = {
            "name": team.name,
            "standup": team.standup.id if team.standup else None,
            "users": [user.username for user in team.user],
        }
        response.append(team_data)
    return jsonify(response)


# Health check for the server
@app.route("/api/health/", methods=["GET"])
@authenticate
def health_check():
    return make_response("Alive!", 200)

