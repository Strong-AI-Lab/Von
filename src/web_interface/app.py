from flask import Flask, render_template, redirect, url_for, request
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_pymongo import PyMongo
from web_interface.user_data import VonUser # work our why it doesn't like this import when it's in the same directory
import argparse

#For details, see https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-111
# write a minimal local web page server, that includes login and identity tracking, and 
# is easy to redeploy on a remote server (e.g. one running on aws or google cloud or Azure). 
# I want to be able to easily call python backend code from the server. 
# Make sure the start code for the local server has an option to run in foreground or background.
app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Replace with a secure secret key in production

# Configure Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' 

# User loader callback
@login_manager.user_loader
def load_user(user_id):
    user = VonUser.find_by_id(user_id) 
    if user:
        return User(user.get_username())
    return None

# User class
class User(UserMixin):
    def __init__(self, username):
        self.vonuser = VonUser(username)
        self.username = username
        self.id = self.vonuser.get_id()
        #str(mongo.db.users.find_one({"username": username})['_id'])

# Routes
@app.route('/')
def home():
    return render_template('home.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        # Verify username and password
        user = VonUser(username)
        if user and user.get_password() == password:  # In production, use hashed passwords
            login_user(User(username))
            return redirect(url_for('dashboard'))
        else:
            return 'Invalid credentials', 401

    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', user=current_user.username)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

# Main function
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run the Flask web server.')
    parser.add_argument('--host', default='127.0.0.1', help='Host to listen on.')
    parser.add_argument('--port', default=5000, type=int, help='Port to listen on.')
    parser.add_argument('--background', action='store_true', help='Run server in the background.')
    args = parser.parse_args()

    if args.background:
        from multiprocessing import Process

        def run_app():
            app.run(host=args.host, port=args.port)

        p = Process(target=run_app)
        p.start()
        print(f"Server running in background on {args.host}:{args.port} (PID: {p.pid})")
    else:
        app.run(host=args.host, port=args.port)
