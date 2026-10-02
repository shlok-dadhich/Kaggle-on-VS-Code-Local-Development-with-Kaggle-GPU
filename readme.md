\# Local Project → Kaggle GPU Development Setup



\## 1. Overview



This setup allows a local Windows project to use a \*\*Kaggle GPU environment\*\* while keeping the source code and files on the local computer.



The goal is:



```text

LOCAL WINDOWS PROJECT

&#x20;       │

&#x20;       │ kaggle-sync

&#x20;       ▼

KAGGLE JUPYTER SERVER

&#x20;       │

&#x20;       ▼

/kaggle/working/local-project

&#x20;       │

&#x20;       │

&#x20;       ├── train.py

&#x20;       ├── model.py

&#x20;       ├── data/

&#x20;       ├── LAB\_1/

&#x20;       └── notebooks/

```



The local project is the main working copy.



Kaggle provides the remote execution environment and GPU.



The user does not need to manually upload every file.



\---



\# 2. What We Wanted



The original problem was:



> "I have my project locally, but I want to run the code using Kaggle's GPU."



For example, the local project might contain:



```text

BT24CSA001\_NLP/

│

├── train.py

├── model.py

├── requirements.txt

│

├── data/

│   ├── train.csv

│   └── test.csv

│

└── LAB\_1/

&#x20;   ├── Assignment 1\_BT24CSA001.ipynb

&#x20;   ├── Assignment 2\_BT24CSA001.ipynb

&#x20;   └── ...

```



The problem is that Kaggle cannot directly see:



```text

C:\\Users\\shlok\\OneDrive\\Documents\\BT24CSA001\_NLP

```



because this directory exists on the local Windows computer.



Kaggle runs on a remote machine.



Therefore, the local project needs to be synchronized to Kaggle.



\---



\# 3. Final Goal



The final workflow we built is:



\## Python files



```powershell

kaggle-sync "KAGGLE\_VSCODE\_URL"

```



Keep synchronization running.



Then, from another terminal:



```powershell

kaggle-run train.py

```



For a Python file inside a folder:



```powershell

kaggle-run LAB\_1\\train.py

```



The local file is automatically synchronized to Kaggle and then executed there.



\---



\# 4. Notebook Workflow



For `.ipynb` files, the workflow is slightly different.



Start synchronization:



```powershell

kaggle-sync "KAGGLE\_VSCODE\_URL"

```



Then open the local notebook in VS Code.



Select the Kaggle Jupyter kernel.



Run notebook cells normally.



For example:



```text

Local VS Code

&#x20;    │

&#x20;    │

&#x20;    └── Assignment 1\_BT24CSA001.ipynb

&#x20;            │

&#x20;            │ Kaggle kernel

&#x20;            ▼

&#x20;      Kaggle GPU

```



There is no need to manually upload the notebook.



\---



\# 5. Main Components



The setup consists of several pieces.



```text

C:\\kaggle-runner\\

│

├── sync.py

├── test-kaggle.py

├── test-upload.py

├── test-run.py

└── ...

```



The important commands are:



```text

kaggle-sync

kaggle-run

```



They are Windows command-line commands configured to run the corresponding Python scripts.



\---



\# 6. `kaggle-sync`



The synchronization program connects the local project to a Kaggle Jupyter server.



Usage:



```powershell

kaggle-sync "KAGGLE\_VSCODE\_URL"

```



Example structure:



```powershell

kaggle-sync "https://kkb-production.jupyter-proxy.kaggle.net/..."

```



The URL comes from the Kaggle Jupyter/VS Code environment.



The URL contains the information required to connect to the active Kaggle Jupyter server.



\---



\# 7. What `kaggle-sync` Does



When started, the program first connects to Kaggle.



It displays:



```text

Connecting to Kaggle Jupyter Server...

Kaggle Jupyter Server: OK

```



Then it performs the initial synchronization.



For example:



```text

============================================================

Initial project synchronization

============================================================



Local : C:\\Users\\shlok\\OneDrive\\Documents\\BT24CSA001\_NLP

Remote: /kaggle/working/local-project

```



The local files are copied to:



```text

/kaggle/working/local-project

```



on Kaggle.



\---



\# 8. Example Initial Synchronization



The project contained files such as:



```text

sync\_test.py

LAB\_1\\Assignment 1\_BT24CSA001.ipynb

LAB\_1\\Assignment 2\_BT24CSA001.ipynb

LAB\_1\\Assignment 2\_BT24CSA001.pdf

LAB\_1\\Assignment 3\_BT24CSA001.ipynb

LAB\_1\\Assignment 3\_BT24CSA001.pdf

LAB\_1\\Assignment 4\_BT24CSA001.ipynb

LAB\_1\\Assignment 4\_BT24CSA001.pdf

LAB\_1\\Assignment\_1.pdf

LAB\_1\\Executed\_Technology\_Services\_Agreement\_Realistic\_Format.pdf

LAB\_1\\Input File.docx

LAB\_1\\input.pdf

LAB\_1\\requirements.txt

LAB\_1\\sentences.txt

```



The synchronization program reported:



```text

Initial synchronization complete: 14 files

```



This confirmed that the local project could be transferred to the remote Kaggle filesystem.



\---



\# 9. Remote Project Location



The remote project is:



```text

/kaggle/working/local-project

```



Therefore:



Local:



```text

C:\\Users\\shlok\\OneDrive\\Documents\\BT24CSA001\_NLP

```



corresponds to:



Kaggle:



```text

/kaggle/working/local-project

```



For example:



```text

LOCAL



C:\\Users\\shlok\\OneDrive\\Documents\\BT24CSA001\_NLP\\train.py

```



becomes:



```text

KAGGLE



/kaggle/working/local-project/train.py

```



\---



\# 10. Automatic Synchronization



After the initial synchronization, `kaggle-sync` stays running.



It displays:



```text

============================================================

KAGGLE AUTOMATIC SYNCHRONIZATION IS RUNNING

============================================================



Local project:

C:\\Users\\shlok\\OneDrive\\Documents\\BT24CSA001\_NLP



Remote project:

/kaggle/working/local-project



Requirements are installed only when their contents change.



Press Ctrl+C to stop.

```



This means the terminal should remain open.



If a local file changes, the synchronization program can synchronize the updated file to Kaggle.



Therefore, the local project remains the primary source.



\---



\# 11. Testing File Synchronization



We first created a small test file.



For example:



```text

sync\_test.py

```



The test confirmed that a local file could be made available on Kaggle.



Inside Kaggle, the file could be checked with:



```python

import os



print(os.path.exists("/kaggle/working/local-project/sync\_test.py"))

```



The expected result is:



```text

True

```



This confirmed that the local project was visible inside Kaggle.



\---



\# 12. Testing the Project Directory



We also checked the project directory:



```python

import os



print(os.path.exists("/kaggle/working/local-project"))

print(os.listdir("/kaggle/working/local-project"))

```



The result showed:



```text

True

```



and files such as:



```text

\[

&#x20;   'sync\_test.txt',

&#x20;   'sync\_test.py',

&#x20;   'LAB\_1'

]

```



This confirmed that the project directory was successfully synchronized.



\---



\# 13. Testing the Kaggle GPU



Inside the Kaggle environment, we tested:



```python

import os

import torch



print(os.getcwd())

print(torch.cuda.is\_available())

print(torch.cuda.device\_count())

```



The result was:



```text

/kaggle/working

True

2

```



Therefore:



```text

CUDA available: True

GPU count: 2

```



The GPUs were:



```text

0 Tesla T4

1 Tesla T4

```



This confirmed that the Kaggle environment was actually providing GPU hardware.



\---



\# 14. Why the GPU Test Matters



The purpose of the setup is not merely to copy files.



The important part is:



```text

Local code

&#x20;   ↓

Kaggle remote environment

&#x20;   ↓

Kaggle GPU

```



For example:



```python

import torch



print(torch.cuda.is\_available())

```



returned:



```text

True

```



and:



```python

print(torch.cuda.device\_count())

```



returned:



```text

2

```



Therefore PyTorch could see both Tesla T4 GPUs.



\---



\# 15. `kaggle-run`



The second major component is:



```text

kaggle-run

```



Its purpose is to make running a local Python file on Kaggle simple.



Instead of manually typing:



```python

%run /kaggle/working/local-project/train.py

```



we can simply run:



```powershell

kaggle-run train.py

```



from Windows.



\---



\# 16. What `kaggle-run` Does



Suppose the local file is:



```text

C:\\Users\\shlok\\OneDrive\\Documents\\BT24CSA001\_NLP\\train.py

```



When we execute:



```powershell

kaggle-run train.py

```



the runner identifies the local project and maps the file to:



```text

/kaggle/working/local-project/train.py

```



It then connects to the Kaggle Jupyter server and executes the Python file remotely.



\---



\# 17. Example `kaggle-run` Output



A successful run looks like:



```text

============================================================

KAGGLE PYTHON RUNNER

============================================================



Local : C:\\Users\\shlok\\OneDrive\\Documents\\BT24CSA001\_NLP\\train.py

Remote: /kaggle/working/local-project/train.py



Connecting to Kaggle...

Kaggle Jupyter Server: OK

Executing on Kaggle...

```



Then Kaggle executes:



```text

/kaggle/working/local-project/train.py

```



\---



\# 18. Execution Environment



The runner also reports information about the remote environment.



For example:



```text

============================================================

RUNNING LOCAL PROJECT ON KAGGLE

============================================================



Script : /kaggle/working/local-project/train.py

CWD    : /kaggle/working/local-project

CUDA   : True

GPUs   : 2

GPU 0 : Tesla T4

GPU 1 : Tesla T4



============================================================

```



This is useful because it proves the Python file is not running on Windows.



It is running inside the Kaggle environment.



\---



\# 19. Example `train.py`



A local file can contain normal Python code.



For example:



```python

import torch



print("HELLO FROM TRAIN.PY")



print("CUDA:", torch.cuda.is\_available())

print("GPU COUNT:", torch.cuda.device\_count())



for i in range(torch.cuda.device\_count()):

&#x20;   print(i, torch.cuda.get\_device\_name(i))

```



Running:



```powershell

kaggle-run train.py

```



produced:



```text

HELLO FROM TRAIN.PY

CUDA: True

GPU COUNT: 2

0 Tesla T4

1 Tesla T4

```



This confirms that the local Python script executed successfully on Kaggle.



\---



\# 20. Running Python Files in Subdirectories



The same system can run files inside folders.



For example:



```text

project/

│

├── train.py

│

├── scripts/

│   ├── preprocess.py

│   └── evaluate.py

│

└── LAB\_1/

&#x20;   └── train.py

```



Run:



```powershell

kaggle-run train.py

```



or:



```powershell

kaggle-run scripts\\preprocess.py

```



or:



```powershell

kaggle-run LAB\_1\\train.py

```



The corresponding remote paths are:



```text

/kaggle/working/local-project/train.py

/kaggle/working/local-project/scripts/preprocess.py

/kaggle/working/local-project/LAB\_1/train.py

```



\---



\# 21. Working Directory



When `kaggle-run` executes a script, the working directory is:



```text

/kaggle/working/local-project

```



This is important.



Suppose the project is:



```text

project/

│

├── train.py

├── data/

│   └── train.csv

└── models/

```



Inside `train.py`, you can use project-relative paths such as:



```python

"data/train.csv"

```



because the script is executed with the project as its working directory.



\---



\# 22. Project Files Become Available Remotely



Suppose the local project contains:



```text

project/

│

├── train.py

├── model.py

├── requirements.txt

│

└── data/

&#x20;   └── train.csv

```



After synchronization:



```text

/kaggle/working/local-project/

│

├── train.py

├── model.py

├── requirements.txt

│

└── data/

&#x20;   └── train.csv

```



Therefore `train.py` can access:



```python

data/train.csv

```



normally.



\---



\# 23. Requirements Support



The synchronization system also detects:



```text

requirements.txt

```



in the project.



For example:



```text

LAB\_1/

└── requirements.txt

```



When the requirements file changes, the synchronization program can install the dependencies on Kaggle.



The system reports:



```text

============================================================

Requirements changed

============================================================



File: LAB\_1\\requirements.txt

Installing dependencies on Kaggle...

```



It then runs the equivalent of:



```bash

pip install -r /kaggle/working/local-project/LAB\_1/requirements.txt

```



inside Kaggle.



\---



\# 24. Requirements Are Not Installed Every Time



The intended behavior is:



```text

requirements.txt unchanged

&#x20;       ↓

Do not reinstall





requirements.txt changed

&#x20;       ↓

Install dependencies again

```



This avoids unnecessarily reinstalling packages every time the synchronization process starts.



\---



\# 25. Important Requirements Consideration



The first requirements installation attempted to install many packages.



For example:



```text

numpy

pandas

spacy

nltk

pydantic

ipython

jupyter\_client

...

```



The installation eventually encountered:



```text

Dependency installation failed:

Connection timed out

```



This was a network/package-installation issue, not a failure of the file synchronization mechanism.



The synchronization process itself continued running.



Later testing confirmed that the project synchronization and Kaggle execution worked correctly.



\---



\# 26. Kaggle Jupyter API Test



Before building the complete synchronization system, we tested the Kaggle Jupyter endpoint.



The diagnostic showed:



```text

Scheme:

https



Host:

kkb-production.jupyter-proxy.kaggle.net

```



The session was successfully identified.



The API test returned:



```text

HTTP status:

200

```



with:



```json

{

&#x20;   "version": "2.12.5"

}

```



This confirmed that the Kaggle Jupyter server was reachable.



\---



\# 27. File Upload API Test



We then tested the Kaggle Contents API.



The test successfully performed:



```text

Create directory: 201

Upload file: 200

Read file: 200

```



A test file was uploaded and read back successfully.



The remote content was:



```text

HELLO FROM WINDOWS SYNC

```



This proved that the local-to-Kaggle file transfer mechanism worked.



\---



\# 28. Initial Problem: BOM Error



Earlier, the runner produced:



```text

Unexpected UTF-8 BOM

(decode using utf-8-sig)

```



This was caused by a UTF-8 BOM in the generated metadata/file being parsed as ordinary UTF-8.



The issue was fixed so that the Kaggle project could be submitted correctly.



\---



\# 29. Important Difference: Kaggle Kernel Submission vs Live Jupyter



Initially, the setup used Kaggle's kernel/project submission mechanism.



That produced something similar to:



```text

Kernel version 1 successfully pushed.

```



However, that was not what we wanted.



The actual requirement was:



```text

Local VS Code

&#x20;      ↓

live synchronization

&#x20;      ↓

Kaggle Jupyter server

&#x20;      ↓

interactive GPU environment

```



Therefore, the system was changed to communicate directly with the Kaggle Jupyter server.



This allowed the local project to behave much more like a remote development environment.



\---



\# 30. Final Architecture



The completed setup can be represented as:



```text

&#x20;                    WINDOWS PC

┌─────────────────────────────────────────────┐

│                                             │

│  C:\\...\\BT24CSA001\_NLP                     │

│                                             │

│  ├── train.py                               │

│  ├── model.py                               │

│  ├── requirements.txt                       │

│  ├── data/                                  │

│  └── LAB\_1/                                 │

│      ├── Assignment 1.ipynb                 │

│      └── ...                                │

│                                             │

│             │                               │

│             │ kaggle-sync                   │

│             ▼                               │

└─────────────┼───────────────────────────────┘

&#x20;             │

&#x20;             │ Internet

&#x20;             ▼

┌─────────────────────────────────────────────┐

│                 KAGGLE                      │

│                                             │

│  Jupyter Server                             │

│                                             │

│  /kaggle/working/local-project/             │

│                                             │

│  ├── train.py                               │

│  ├── model.py                               │

│  ├── requirements.txt                       │

│  ├── data/                                  │

│  └── LAB\_1/                                 │

│                                             │

│       CUDA = True                           │

│       GPU 0 = Tesla T4                     │

│       GPU 1 = Tesla T4                     │

│                                             │

└─────────────────────────────────────────────┘

```



\---



\# 31. Normal Daily Workflow



\## Step 1 — Open the project



Open the local project in VS Code.



Example:



```text

C:\\Users\\shlok\\OneDrive\\Documents\\BT24CSA001\_NLP

```



\---



\## Step 2 — Start synchronization



Open a terminal:



```powershell

kaggle-sync "YOUR\_KAGGLE\_VSCODE\_URL"

```



Do not close this terminal.



You should see:



```text

Kaggle Jupyter Server: OK

```



and:



```text

KAGGLE AUTOMATIC SYNCHRONIZATION IS RUNNING

```



\---



\# 32. Running a `.py` File



Open a second terminal.



For:



```text

train.py

```



run:



```powershell

kaggle-run train.py

```



For:



```text

LAB\_1\\train.py

```



run:



```powershell

kaggle-run LAB\_1\\train.py

```



The program executes on Kaggle.



\---



\# 33. Running an `.ipynb` File



For notebooks:



1\. Start `kaggle-sync`.

2\. Open the local `.ipynb` in VS Code.

3\. Select the Kaggle Jupyter kernel.

4\. Run notebook cells normally.



Example:



```text

LAB\_1/

└── Assignment 1\_BT24CSA001.ipynb

```



The notebook remains local, while its execution kernel is on Kaggle.



\---



\# 34. Two-Terminal Setup



The recommended setup is:



\### Terminal 1



```powershell

kaggle-sync "KAGGLE\_VSCODE\_URL"

```



Keep it running.



\### Terminal 2



For Python:



```powershell

kaggle-run train.py

```



For another Python file:



```powershell

kaggle-run LAB\_1\\some\_script.py

```



For notebooks, use the VS Code notebook interface with the Kaggle kernel.



\---



\# 35. Stopping Synchronization



To stop the synchronization program:



```text

Ctrl + C

```



The terminal will stop the live synchronization process.



\---



\# 36. What Not to Do



You normally do not need to manually run:



```python

%run /kaggle/working/local-project/train.py

```



when using:



```powershell

kaggle-run train.py

```



You also do not need to manually upload every Python file.



The synchronization program handles the local-to-remote file transfer.



\---



\# 37. Simple Mental Model



Remember only this:



```text

kaggle-sync

```



means:



> "Keep my local project available on Kaggle."



And:



```text

kaggle-run train.py

```



means:



> "Run this local Python file on the Kaggle GPU."



For notebooks:



```text

kaggle-sync

&#x20;       +

Kaggle kernel in VS Code

```



means:



> "Use the Kaggle GPU interactively with my local notebook."



\---



\# 38. Final Commands Cheat Sheet



\## Start synchronization



```powershell

kaggle-sync "KAGGLE\_VSCODE\_URL"

```



\---



\## Run Python file



```powershell

kaggle-run train.py

```



\---



\## Run Python file in a folder



```powershell

kaggle-run LAB\_1\\train.py

```



\---



\## Run another script



```powershell

kaggle-run scripts\\preprocess.py

```



\---



\## Notebook



```text

kaggle-sync "KAGGLE\_VSCODE\_URL"



→ Open .ipynb

→ Select Kaggle kernel

→ Run cells

```



\---



\## Stop sync



```text

Ctrl + C

```



\---



\# 39. Verification Commands



Inside Kaggle, verify the GPU:



```python

import torch



print("CUDA:", torch.cuda.is\_available())

print("GPU COUNT:", torch.cuda.device\_count())



for i in range(torch.cuda.device\_count()):

&#x20;   print(i, torch.cuda.get\_device\_name(i))

```



Expected environment from our successful test:



```text

CUDA: True

GPU COUNT: 2

0 Tesla T4

1 Tesla T4

```



Verify the project:



```python

import os



print(os.path.exists("/kaggle/working/local-project"))

print(os.listdir("/kaggle/working/local-project"))

```



\---



\# 40. Troubleshooting



\## `Kaggle Jupyter Server: OK`



This means the connection is working.



Continue using the setup.



\---



\## File does not appear on Kaggle



Check that:



```powershell

kaggle-sync "URL"

```



is still running.



Then check:



```python

import os



print(os.listdir("/kaggle/working/local-project"))

```



\---



\## `kaggle-run` says the file does not exist



Make sure the path is relative to the project.



Correct:



```powershell

kaggle-run train.py

```



or:



```powershell

kaggle-run LAB\_1\\train.py

```



\---



\## CUDA is `False`



Check the Kaggle session and GPU configuration.



The successful setup previously showed:



```text

CUDA: True

GPU COUNT: 2

```



\---



\## Requirements installation times out



A timeout during:



```text

pip install -r requirements.txt

```



can occur because packages must be downloaded from the internet.



This does not necessarily mean that synchronization is broken.



Check whether the required packages were already installed.



\---



\# 41. Security Note



The Kaggle Jupyter URL contains authentication information required to access the Jupyter server.



Treat it as sensitive.



Do not post the complete URL publicly or commit it to Git.



If a Jupyter URL/token has been exposed publicly, obtain a fresh session/URL before continuing with sensitive work.



\---



\# 42. Final Result



The setup now provides the desired workflow:



```text

&#x20;                 LOCAL DEVELOPMENT

&#x20;                        │

&#x20;                        │

&#x20;                   edit files

&#x20;                        │

&#x20;                        ▼

&#x20;                 kaggle-sync

&#x20;                        │

&#x20;                        ▼

&#x20;               KAGGLE JUPYTER

&#x20;                        │

&#x20;                        ▼

&#x20;                   GPU EXECUTION

&#x20;                        │

&#x20;             ┌──────────┴──────────┐

&#x20;             │                     │

&#x20;             ▼                     ▼

&#x20;         Python .py            Notebook .ipynb

&#x20;             │                     │

&#x20;      kaggle-run file.py      Select Kaggle kernel

&#x20;             │                     │

&#x20;             └──────────┬──────────┘

&#x20;                        ▼

&#x20;                   Tesla T4 GPU

```



The key commands are therefore:



```powershell

kaggle-sync "URL"

```



and:



```powershell

kaggle-run train.py

```



For notebooks:



```text

kaggle-sync "URL"

→ open notebook

→ select Kaggle kernel

→ run

```



This gives a local-project development workflow while using Kaggle's remote GPU environment for execution.

