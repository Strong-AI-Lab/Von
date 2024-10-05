from flask import Flask, render_template, redirect, url_for, request
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_pymongo import PyMongo
from bson.objectid import ObjectId
import argparse

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Replace with a secure secret key in production

# Configure MongoDB
app.config["MONGO_URI"] = "mongodb://localhost:27017/yourdbname"  # Change this for your MongoDB URI
mongo = PyMongo(app)

# Configure Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# User loader callback
@login_manager.user_loader
def load_user(user_id):
    user = mongo.db.users.find_one({"_id": ObjectId(user_id)})
    if user:
        return User(user['username'])
    return None

# User class
class User(UserMixin):
    def __init__(self, username):
        self.username = username
        self.id = str(mongo.db.users.find_one({"username": username})['_id'])

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
        user = mongo.db.users.find_one({"username": username})
        if user and user['password'] == password:  # In production, use hashed passwords
            login_user(User(username))
            return redirect(url_for('dashboard'))
        else:
            return 'Invalid credentials', 401

    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', user=current_user.id)

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
