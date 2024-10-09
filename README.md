## Setup
Before attempting to run feedbot-website, ensure that you do the following:

    Add .env to project directory:
Create a file ".env" and add the following to it:

"
AUTHORIZE_URL= *authorization_link*
TOKEN_URL= *token_link*
CLIENT_SECRET= *secret_key*
CLIENT_ID= *client_id*
FEEDBOT_KEY= *something you choose*
OPENAI_KEY=sk-... 
"

Note the FEEDBOT_KEY can be anything; whatever you choose just has to match what
you pass in the `--key` argument when you invoke the feedbot script. For local
testing, this can be something simple, like 123.

    Get localhost running:
Install apache from their website and get the localhost server running. When you navigate to localhost on a browser, you should get "It works!" in bold text. 


    Install and setup postgresql:
Postgresql is an open-source database management system that uses SQL queries. Install it to your system from their website. 

Once installed, set up the development database (note: this assumes you can connect as an admin to your local postgres, and that `postgres` is a database that already exists;
if you connect in some other way, figure out how to run the SQL commands in that file):

``` shell
psql postgres < init.sql

```

Alternatively, you can navigate to "SQL Shell (psql)". It should be in the same folder that is installed when installing postgresql. In the shell, use default server, database, port, and username, and execute the commands in init.sql line by line.


Now, you can run the application by dong the following: 

Create virtual environment:
``` shell
python -m venv .venv
```

On new development terminal:

``` shell
source .venv/bin/activate
```


Install from `requirements.txt`:

``` shell
pip install -r requirements.txt
```


Run the app:

``` shell
flask run
```

Or run with

``` shell 
python app.py
```



## Accessing feedback
Only course staff emails and the student's email who submitted the assignment
are permitted to access automated feedback for an assignment, so add your email
to `staff` table in the database to access feedback (there is currently no UI
for this; do it using `psql`).

To authorise yourself, use http://localhost:5001/login, and login with the same
email as was added to the staff table. You can then visit http://localhost:5001/
to access feedback.


If login authorisation doesn't work, you can test a submission by tacking on
your email as the submitter email when running the "feedbot" application. Note:
This only works if you were already logged in to dbp.io with that email.


