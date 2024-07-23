# Von Component: paper_recommender

This is a project started at Strong AI Lab that aimed to route papers shared in the Slack channel to different people based on their current projects, and at the same time serve as a platform for testing research done in the lab and data collection. 

The main features currently are
- Slack application as the user interface.
- Record project descriptions. Each user can have multiple projects.
- Route papers shared in the channels the bot is installed in to different users based on their project descriptions.
  - The bot reads messages posted in a channel that the bot is installed in
  - Extracts any URLs in the message
  - Obtain paper information such as title, abstract and authors either through API or web scraping.
  - The extracted abstract is compared against each project in the database. A private message is sent to a user if the paper is deemed relevant along with an explanation on the decision. Currently, OpenAI API is used as the "engine".
    - A user can optionally choose to receive messages even if the paper is deemed irrelevant.  
    - These messages include UI for collecting user feedback.
- All the data are recorded in a MongoDB database.
- User can query papers in the databse using keyword search in the "Messages" tab of the paper_recommender app. It returns 5 papers by default.
  -  The search will search for papers whose title, author list or abstract match the provided keywords.
  -  If the query is a list of terms separated by space, for example `store shop`,then papers that contain any of the terms in the query will be a match.
  -  To search for papers that contain a specific phrase or name, encapsulate the term with quotation marks, e.g. `"deep learning"`.
-  By default, the app will log unknown domains from extracted URLs and other app exceptions or warnings under `logs\` directory in the root directory of the repo. This directory will be automatically created when a log is triggered.
 

## Structure 
- `src/paper_recommender` contains all the source code of the project. Under it
  - `app.py` is the app entry point. It contains all app and server related functions.
  - `slack_templates` contains message and UI templates for the Slack app.
  - `engine` is where AI related stuff is located. Currently, only OpenAI API is used.
  - `mangodb` contains all mongodb related operations.
  - `paper_extraction` contains code for extracting paper information from different sources.
- `configs` is used for configuration settings.


## Prerequisites
- OpenAI API key
  - A new key can be generated here: https://platform.openai.com/api-keys
  - Set up the `OPENAI_API_KEY` by following the instruction [here](https://platform.openai.com/docs/quickstart/step-2-set-up-your-api-key). Please use the recommended approach "Set up your API key for all projects" to avoid putting sensitive information like the key in the code repo.
- Slack authentication tokens and app configurations
  - You can access the administration portal for an existing app or create a new app here: https://api.slack.com/apps
  - `SLACK_BOT_TOKEN` is required to authenticate the app with Slack. It can be found under "OAuth & Permissions" in the left panel of the App adminstration portal.
  - `SLACK_APP_TOKEN` is required for socket mode communication with Slack (this is the current implementation). It can be generated under "Basic Information" from the left panel of the administration portal.
    - Socket mode uses web socket protocol instead of standard HTTP Web API. It establishes a simultaneous two-way communication channel that stays alive. This is useful for development and deployment within a corporate fire wall. However, it doesn't allow distribute the app in the Slack App Directory. If distributing the app in the App Directory is required, then the standard HTTP Web API should be used instead of socket mode implementation.
  - `SLACK_SIGNING_SECRET` is only required if the standard HTTP Web API mode is implemented instead of socket mode. It can be found under "Basic Information" as well.
  - All of these tokens should be placed in the same place as environment variables just like the OpenAI API key - the same instruction applies here.
  - If a new app needs to be created, the provided `manifest.json` file can be used to set the correct configurations, especially for app permissions and event subscriptions. Instructions can be found here: https://api.slack.com/reference/manifests
- MongoDB
  - Installing MongoDB for your OS: https://www.mongodb.com/docs/manual/administration/install-community/
  - Installing MongoDB Compass, a GUI tool for MongoDB: https://www.mongodb.com/docs/compass/master/install/
  - The database should be automatically created and populated once the app is running.
  - However, the code currently doesn't automatically create indices for the collections. This has to be done manually either programmatically or through the Compass GUI. The metadata about the keys for each collection can be found in the `mongodb_metadata` directory. Information about how indexing and how to create indices can be found here: https://www.mongodb.com/docs/manual/core/indexes/create-index/
 
## Installation
This repository was developed and tested on Ubuntu using Python3.8. However, other OS and newer Python version might still work. 
1. Create a virtual environment by running `python3.8 -m venv <directory>`. You can try using your desired version of Python, ideally at least Python3.8. You need to make sure the specified Python version is already installed. `<directory>` is your desired place to store this environment.
2. In a different directory than your environment, clone this repo by using your choice of GUI client or by running `git clone https://github.com/Strong-AI-Lab/paper_recommendation.git`
3. Activate the environment - it is different depending on OS. Instructions can be found here: https://python.land/virtual-environments/virtualenv
4. Install all required packages by running `pip install -r requirements.txt`. Make sure you run this command in the root directory of this repo.
5. Install this repo from source by running the following command in the root directory of this repo.
  - `pip install --editable .` - this will install this repo in development mode, so that changes make to the code will be reflected in the installed app once you restart it. Or
  - `pip install .` - This is usually for deployment where any changes to the source code will not have any effect on the installed app.
6. Configurations in `configs/configs.ini`
  -  The most important configuration is the database connection string `mongodb_connect_str`. The default value is usually correct if the mongodb datase is deployed locally on the same machine.
  -  You can also change the OpenAI API model by changing the value for `model`.
  -  You can optionally change the file path for logging unknown domains or other app warnings or exceptions.

## Start APP
Once all the required packages have been installed, start the app by running `paper_recommender` in the terminal. 

## Notes for Further Development and Open Source
- The App server is implemented using Socket Mode, which is very convenient for development and deployment behind a corporate firewall. However, if distribution of the app in the Slack App Directory is desirable, then the implementation has to be changed to HTTP Web API mode.
- The App is currently implemented in synchonous mode. This is Okay for a small user base. However, if the app is to be developed with larger user group in mind, the server and some of the functions and operations need to be changed to asynchronous implementations.
- The community version of MongoDB does not have vector store. If vector store is required, then a separate vector database should be implemented.
- The App was originally developed soley with internal use in mind, so I went a bit crazy with the "Star Wars" theme... Sorry about that! If this repo is to be developed further for open source and distribution, then any messaging, description, image and even the package name need to be edited to avoid copyright infringement.

- I suggest create a brand new copy of the app and database by following this readme. It serves two purposes:
  - It helps find out problems in this readme.
  - There should ideally be two copies of the app and database. One for deployment, another one for development. Only the deployment version should be installed to public channels like the literature channel. The development version can be installed to a new channel dedicated to development.
- I suggest avoid pushing any commits directly into the `main` branch. When developing new features, use the `dev` branch instead, or creating new branches. Then you can submit pull request to merge the changes into the `main ` branch.