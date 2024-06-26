from __future__ import annotations

import os
import secrets
import requests
from typing import List
from random import randint
from urllib.parse import urlencode
import json
from dotenv import load_dotenv
import uuid
import re

from flask import Flask, redirect, request, url_for, session, current_app, abort, flash, render_template

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    load_only,
    mapped_column,
    relationship,
)
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID

# NOTE(dbp 2024-02-06): bit of a hack; probably better to do this with a .env file
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = "postgresql://feedbot_user:111@localhost/feedbot_dev"
else:
    # SQLAlchemy does not support postgres: url strings, but it seems that fly.io produces them...
    os.environ["DATABASE_URL"] = re.sub("postgres:","postgresql:",os.environ["DATABASE_URL"])

# NOTE(dbp 2024-04-09): Not sure how else to see what the DB they create for us is called...
print(os.environ["DATABASE_URL"],flush=True)

class Base(DeclarativeBase):
    pass

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = "some secret for session"
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
app.config["OAUTH2"] = {
    "client_id": os.environ.get("CLIENT_ID"),
    "client_secret": os.environ.get("CLIENT_SECRET"),
    "redirect_url": "http://localhost:5000/auth",
    "authorize_url": os.environ.get("AUTHORIZE_URL"),
    "token_url": os.environ.get("TOKEN_URL"),
    "user_info_url": "https://graph.microsoft.com/v1.0/me?$select=employeeId,mail",
    "scopes": ["openid", "email", "profile", "offline_access", "User.Read"],
}

db = SQLAlchemy(model_class=Base)
db.init_app(app)


class Submission(db.Model):
    __tablename__ = "submissions"

    id = db.Column(UUID(as_uuid=True), primary_key=True, unique=True, default=uuid.uuid4)
    sso: Mapped[str]
    code: Mapped[str]
    comments: Mapped[List["Comment"]] = relationship()


class Comment(db.Model):
    __tablename__ = "comments"

    comment_id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    line_number: Mapped[int]
    text: Mapped[str]
    subm_id = mapped_column(ForeignKey("submissions.id"))

    def __repr__(self):
        return f'Comment(line_number: "{self.line_number}", text: "{self.text}")'


with app.app_context():
    db.create_all()


@app.route("/")
def hello_world():
    if session.get("email"):
        return f"<p>Hello, {session['email']}, this is your nuid {session['nuid']}</p> "
    else:
        return "Hello world!"


@app.route("/authorize")
def oauth2_authorize():
    if session.get("email"):
        return redirect(url_for("hello_world"))

    session["oauth2_state"] = secrets.token_urlsafe(16)

    oauth = current_app.config["OAUTH2"]

    # create a query string with all the OAuth2 parameters
    qs = urlencode(
        {
            "client_id": oauth["client_id"],
            "redirect_uri": url_for("oauth2_callback", _external=True),
            "response_type": "code",
            "scope": " ".join(oauth["scopes"]),
            "state": session["oauth2_state"],
        }
    )

    # redirect the user to the OAuth2 provider authorization URL
    return redirect(oauth["authorize_url"] + "?" + qs)


@app.route("/auth")
def oauth2_callback():
    if session.get("email"):
        return redirect(url_for("hello_world"))

    oauth = current_app.config["OAUTH2"]

    # if there was an authentication error, flash the error messages and exit
    if "error" in request.args:
        for k, v in request.args.items():
            if k.startswith("error"):
                flash(f"{k}: {v}")
        return redirect(url_for("hello_world"))

    # make sure that the state parameter matches the one we created in the
    # authorization request
    if request.args["state"] != session.get("oauth2_state"):
        abort(401)

    # make sure that the authorization code is present
    if "code" not in request.args:
        abort(401)

    # exchange the authorization code for an access token
    response = requests.post(
        oauth["token_url"],
        data={
            "client_id": oauth["client_id"],
            "client_secret": oauth["client_secret"],
            "code": request.args["code"],
            "grant_type": "authorization_code",
            "redirect_uri": url_for("oauth2_callback", _external=True),
        },
        headers={"Accept": "application/json"},
    )
    if response.status_code != 200:
        abort(401)
    oauth2_token = response.json().get("access_token")
    if not oauth2_token:
        abort(401)

    # use the access token to get the user's email address
    response = requests.get(
        oauth["user_info_url"],
        headers={
            "Authorization": "Bearer " + oauth2_token,
            "Accept": "application/json",
        },
    )
    if response.status_code != 200:
        abort(401)

    print(response.json())

    email = response.json()["mail"]
    nuid = response.json()["employeeId"]

    session["email"] = email
    session["nuid"] = nuid

    return redirect(url_for("hello_world"))

class CodeCommentPair:
    def __init__(self, code, comment):
        self.code = code
        self.comment = comment

def get_code_com_pairs(submission):
    result = []
    last_comment_line = 0

    for comment in submission.comments:
        code_block = ""
        for code_line in submission.code.split('\n')[last_comment_line:comment.line_number]:
            code_block += code_line + '\n'
        result.append(CodeCommentPair(code_block.strip(), comment.text))
        last_comment_line = comment.line_number

    remaining_code = ""
    for code_line in submission.code.split('\n')[last_comment_line:]:
        remaining_code += code_line + '\n'
    if remaining_code.strip():
        result.append(CodeCommentPair(remaining_code.strip(), None))

    return result

@app.route("/submission/<id>")
@app.route("/submission/<id>")
def submission(id):
    submission = db.get_or_404(Submission, id)
    return render_template(
        "submission_view.html.jinja",
        code_pairs=get_code_com_pairs(submission)
    )

@app.route("/entry", methods=["POST"])
def receive_entry():
    data = request.args
    print(f"data: {data}")
    id = 0
    if validate(data):
        submission, id = transform(data)
        print(f"id: {id}")
        db.session.add(submission)
        db.session.commit()
    return {"msg": f"id: {id}"}, 200


# this will eventually validate that the sender of an entry is us,
# presumably by using a shared key
def validate(data):
    return True


# this will eventually change the received entry to contain properly
# formatted data (I think just exchanging NUID or email for an SSO token?)
def transform(data):
    # BAD: DO NOT DO PURE RANDOM FOR ID GEN
    gen_id = uuid.uuid4()

    comment_json_list = json.loads(data["comments"])["comments"]
    comment_list = []
    for com_json in comment_json_list:
        comment_list.append(
            Comment(
                line_number=com_json["line_number"],
                text=com_json["text"],
                subm_id=gen_id,
            )
        )

    return (
        Submission(
            # TODO: actual ID assignment
            id=gen_id,
            # TODO: SSO assignment
            sso="",
            code=data["code"],
            comments=comment_list,
        ),
        gen_id,
    )


if __name__ == "__main__":
    app.run(host="localhost", debug=True, port=5001)
