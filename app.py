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
from flask_wtf import FlaskForm
from wtforms import SelectField, SubmitField
WTF_CSRF_ENABLED = False
from werkzeug.middleware.proxy_fix import ProxyFix

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

staff = json.loads(open("staff.json").read())

class FeedbackDropdown(FlaskForm):
    class Meta:
        csrf = False

    feedback_choice = SelectField(u"How useful did you find this comment?", choices=[('very', 'Very useful'), ('some', 'Somewhat useful'), ('no', 'Not helpful')])
    submit = SubmitField("Submit")
    
    def __init__(self, comment_id, *args, **kwargs):
        self.comment_id = comment_id
        super(FlaskForm, self).__init__(*args, **kwargs)
    
       
def make_feedback_form(comment_ids, req_form):
    """Returns a list of feedback dropdowns, with each form corresponding to an id in comment_ids"""
    forms = []
    for id in comment_ids:
        forms.append(FeedbackDropdown(id, req_form))
    
    return forms

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


class Submission(db.Model):
    __tablename__ = "submissions"

    id = db.Column(UUID(as_uuid=True), primary_key=True, unique=True, default=uuid.uuid4)
    email: Mapped[str]
    comments: Mapped[List["Comment"]] = relationship()


class Comment(db.Model):
    __tablename__ = "comments"

    comment_id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    text: Mapped[str]
    code: Mapped[str]
    path: Mapped[str]
    subm_id = mapped_column(ForeignKey("submissions.id"))

    def __repr__(self):
        return f'Comment(line_number: "{self.line_number}", text: "{self.text}")'


with app.app_context():
    db.create_all()

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

@app.route("/submission/<id>", methods=["GET", "POST"])
def submission(id):
    if "email" not in session:
        session["redirect_to"] = request.full_path
        return oauth2_login()

    submission = db.get_or_404(Submission, id)
    if (submission.email != session["email"]) and (session["email"] not in staff):
        return render_template("unavailable.html.jinja")
    
    comment_ids = [comment.comment_id for comment in submission.comments]
    feedback_form = make_feedback_form(comment_ids, request.form)

    
    feedback_comment = None # comment for which feedback was given
    id = None # id of the above comment 
    for key in request.form.keys():
        if request.form[key] == "Submit":
            id = key
            print(key in [str(x) for x in comment_ids])

    
    for comment in submission.comments:
        if str(comment.comment_id) == id:
            feedback_comment = comment 



    #Default all dropdowns to "very useful" everytime a new form submission is made
    for dropdown in feedback_form:
        dropdown.feedback_choice.data = "very"
           

    return render_template(
        "submission_view.html.jinja",
        submission = submission,
        comments_and_forms = zip(submission.comments, feedback_form) 
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


def transform(data):
    # BAD: DO NOT DO PURE RANDOM FOR ID GEN
    gen_id = uuid.uuid4()

    comment_json_list = json.loads(data["comments"])["comments"]
    comment_list = []
    for com_json in comment_json_list:
        comment_list.append(
            Comment(
                text=com_json["text"],
                code=com_json["code"],
                path=com_json["path"],
                subm_id=gen_id,
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
