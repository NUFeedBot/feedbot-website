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
from threading import Thread
from werkzeug.middleware.proxy_fix import ProxyFix

import asyncio
from openai import AsyncOpenAI

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from flask import Flask, redirect, request, url_for, session, current_app, abort, flash, render_template

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    load_only,
    mapped_column,
    relationship,
)

from typing import Optional

from sqlalchemy import ForeignKey, Integer, DateTime, String
from sqlalchemy.sql import func
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
    "authorize_url": os.environ.get("AUTHORIZE_URL"),
    "token_url": os.environ.get("TOKEN_URL"),
    "user_info_url": "https://graph.microsoft.com/v1.0/me?$select=employeeId,mail",
    "scopes": ["openid", "email", "profile", "offline_access", "User.Read"],
}
# Fix for redirects not using https
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)



db = SQLAlchemy(model_class=Base)
db.init_app(app)

class Waiting(db.Model):
    __tablename__ = "waitings"

    id = db.Column(UUID(as_uuid=True), primary_key=True, unique=True, default=uuid.uuid4)
    email: Mapped[str]
    model: Mapped[str]
    parts: Mapped[List["Part"]] = relationship(back_populates="waiting")
    started_at  = db.Column(DateTime(timezone=True))

class Part(db.Model):
    __tablename__ = "parts"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    path: Mapped[str]
    prompt: Mapped[str]
    delimiter: Mapped[str]
    code: Mapped[str]
    waiting_id = mapped_column(ForeignKey("waitings.id"))
    waiting: Mapped["Waiting"] = relationship(back_populates="parts")


class Submission(db.Model):
    __tablename__ = "submissions"

    id = db.Column(UUID(as_uuid=True), primary_key=True, unique=True, default=uuid.uuid4)
    email: Mapped[str]
    comments: Mapped[List["Comment"]] = relationship(back_populates="submission")
    created_at = db.Column(DateTime(timezone=True), server_default=func.now())

class Comment(db.Model):
    __tablename__ = "comments"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    text: Mapped[str]
    code: Mapped[str]
    path: Mapped[str]
    submission_id = mapped_column(ForeignKey("submissions.id"))
    submission: Mapped["Submission"] = relationship(back_populates="comments")
    feedbacks : Mapped[List["Feedback"]] = relationship(back_populates="comment")


    def __repr__(self):
        return f'Comment(line_number: "{self.line_number}", text: "{self.text}")'


class Viewed(db.Model):
    __tablename__ = "viewed"

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    submission_id = mapped_column(ForeignKey("submissions.id"))
    viewed_at = db.Column(DateTime(timezone=True), server_default=func.now())

class Feedback(db.Model):
    __tablename__ = "feedback"

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    comment_id = mapped_column(ForeignKey("comments.id"))
    comment: Mapped["Comment"] = relationship(back_populates="feedbacks")
    added_at = db.Column(DateTime(timezone=True), server_default=func.now())
    rating = db.Column(String)

class Staff(db.Model):
    __tablename__ = "staff"

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str]

with app.app_context():
    db.create_all()

    global staff
    staff = [s.email for s in db.session.query(Staff).all()]

def redirect_back():
    if "redirect_to" in session:
        return redirect(session["redirect_to"])
    else:
        return redirect(request.referrer)

@app.route("/")
def index():
    return render_template(
        "index.html.jinja",
        session=session
    )


@app.route("/login")
def oauth2_login():
    if "email" in session:
        return redirect_back()

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

@app.route("/logout")
def oauth2_logout():
    if "email" in session:
        del session["email"]
    return redirect_back()


@app.route("/auth")
def oauth2_callback():
    if "email" in session:
        return redirect(session["redirect_to"])

    oauth = current_app.config["OAUTH2"]

    # if there was an authentication error, flash the error messages and exit
    if "error" in request.args:
        for k, v in request.args.items():
            if k.startswith("error"):
                flash(f"{k}: {v}")
        return redirect_back()

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

    email = response.json()["mail"]
    nuid = response.json()["employeeId"]

    session["email"] = email
    session["nuid"] = nuid

    if "redirect_to" in session:
        target = session["redirect_to"]
        del session["redirect_to"]
        return redirect(target)
    else:
        return redirect("/")

@app.route("/feedback/<id>/<rating>", methods=["POST"])
def feedback(rating,id):
    if "email" not in session:
        session["redirect_to"] = request.full_path
        abort(401)

    # Not really "unauthorized", but if they are trying to form hack, close enough.
    if rating not in ["great", "okay", "useless"]:
        abort(401)

    comment = db.get_or_404(Comment, id)
    if comment.submission.email != session["email"]:
        abort(401)

    db.session.add(Feedback(comment_id=comment.id, rating=rating))
    db.session.commit()

    return f"<strong>FeedBot Acknowledged; my comments were <mark>{rating}</mark>.</strong> <button hx-post=\"/feedback-undo/{comment.id}\" hx-target=\"#feedback-{comment.id}\">I didn't mean that</button>"

@app.route("/feedback-undo/<id>", methods=["POST"])
def feedback_undo(id):
    if "email" not in session:
        session["redirect_to"] = request.full_path
        abort(401)

    comment = db.get_or_404(Comment, id)
    if comment.submission.email != session["email"]:
        abort(401)

    for f in comment.feedbacks:
        db.session.delete(f)
    db.session.commit()

    # This should be refactored to not copy what is in the template
    return f"""
      <button hx-post="/feedback/{comment.id}/great" hx-target="#feedback-{comment.id}">Very Helpful</button>
      <button hx-post="/feedback/{comment.id}/okay" hx-target="#feedback-{comment.id}">Somewhat Helpful</button>
      <button hx-post="/feedback/{comment.id}/useless" hx-target="#feedback-{comment.id}">Not Helpful</button>"""



@app.route("/submission", methods=["POST"])
def add_submission():
    data = request.get_json()
    if validate(data):

        wid = uuid.uuid4()
        plist = []
        for p in data["parts"]:
            if "delimiter" in p:
                d = p["delimiter"]
            else:
                d = None
            plist.append(
                Part(
                    path=p["path"],
                    prompt=p["prompt"],
                    code=p["code"],
                    delimiter=d,
                    waiting_id = wid
                )
            )

        w = Waiting(id = wid,
                    email=data["email"],
                    model=data["model"],
                    parts=plist)

        db.session.add(w)
        db.session.commit()
        return {"id": f"{w.id}"}, 200
    else:
        abort(401)

async def resolve_part(client, model, part):
    messages = [{"role": "user", "content": part.prompt}]
    chat_completion = await client.chat.completions.create(
            messages=messages,
            model=model)
    res = chat_completion.choices[0].message.content
    if part.delimiter:
        cut = cut_at_delimiter(res, part.delimiter)
    else:
        cut = res
    return {"text": redact_codeblocks(cut),
            "code": part.code,
            "path": part.path}

async def resolve_all(client, model, parts):
    return await asyncio.gather(*[resolve_part(client, model, p) for p in parts], return_exceptions=True)

def cut_at_delimiter(text, delimiter):
    sides = text.split(delimiter)
    if len(sides) < 2: return "FeedBot got confused. We're sorry!"
    return sides[-1]

def redact_codeblocks(text):
    # Regular expression pattern to match markdown code blocks
    codeblock_pattern = r'```(?:.*)\n([\s\S]*?)```'
    redacted_text = re.sub(codeblock_pattern, "[CODE REDACTED]", text)
    return redacted_text


def resolve_waiting(id, app_context):
    app_context.push()

    print(f"RESOLVING {id}")

    w = db.session.get(Waiting, id)
    w.started_at = datetime.now(timezone.utc)

    db.session.commit()

    print("Marked started")


    key = os.environ["OPENAI_KEY"]

    client = AsyncOpenAI(api_key=key)

    comments = asyncio.run(resolve_all(client, w.model, w.parts))

    print(comments)

    comment_list = []
    for com in comments:
        comment_list.append(
            Comment(
                text=com["text"],
                code=com["code"],
                path=com["path"],
                submission_id=id,
            )
        )

    sub = Submission(id=id,
                     email=w.email,
                     comments=comment_list)
    db.session.add(sub)
    db.session.commit()


@app.route("/submission/<id>", methods=["GET"])
def submission(id):
    if "email" not in session:
        session["redirect_to"] = request.full_path
        return oauth2_login()

    submission = Submission.query.get(id)

    if submission is None:
        waiting = db.get_or_404(Waiting, id)
        if (waiting.email != session["email"]) and (session["email"] not in staff):
            return render_template("unavailable.html.jinja")

        if waiting.started_at is None or (waiting.started_at + timedelta(minutes=1) < datetime.now(timezone.utc)):
            Thread(target=resolve_waiting, args=(id, current_app.app_context(),), daemon=True).start()

        return render_template(
            "waiting_view.html.jinja",
            waiting = waiting
        )

    else:
        if (submission.email != session["email"]) and (session["email"] not in staff):
            return render_template("unavailable.html.jinja")

        if submission.email == session["email"]:
            db.session.add(Viewed(submission_id=submission.id))
            db.session.commit()

        return render_template(
            "submission_view.html.jinja",
            submission = submission
        )



@app.route("/entry", methods=["POST"])
def receive_entry():
    data = request.get_json()
    if validate(data):
        submission, id = transform(data)
        print(f"id: {id}")
        db.session.add(submission)
        db.session.commit()
        return {"msg": f"id: {id}"}, 200
    else:
        abort(401)



# this will eventually validate that the sender of an entry is us,
# presumably by using a shared key
def validate(data):
    key = os.environ["FEEDBOT_KEY"]
    return key == data['key']


def transform(data):
    gen_id = uuid.uuid4()

    comment_json_list = data["comments"]["comments"]
    comment_list = []
    for com_json in comment_json_list:
        comment_list.append(
            Comment(
                text=com_json["text"],
                code=com_json["code"],
                path=com_json["path"],
                submission_id=gen_id,
            )
        )

    return (
        Submission(
            # TODO: actual ID assignment
            id=gen_id,
            email=data["email"],
            comments=comment_list,
        ),
        gen_id,
    )


if __name__ == "__main__":
    app.run(host="localhost", debug=True, port=5001)
