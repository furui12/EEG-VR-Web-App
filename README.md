# EEG-VR-Web-App
A program to visualize and pre-process EEG data in real time that is streamed through LSL while synchronizing it with stimulus events from VR neuroadaptive prorgrams also publishing data via LSL. Additionally allows users to manage subject and trial data and metadata.

## How to Use
To start interacting with the web app user interface, first clone the repository into the directory of your choice and install the latest version of the Python modules needed for this project either via the requirements file or the command below:

`pip install --update fastapi uvicorn pydantic sqlalchemy numpy scipy pylsl plotly`

Then, change the active directory to v0.1.1 and run the following command using uvicorn:

`uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
