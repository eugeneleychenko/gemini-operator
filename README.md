# Operator — Universal Web Task Agent

> **Category:** UI Navigator ☸️ | Gemini Live Agent Challenge

A general-purpose web automation agent that navigates websites visually — using Gemini's multimodal vision to understand screenshots and execute actions like a human would. No APIs, no DOM parsing, pure visual understanding.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│   Browser    │────▶│   FastAPI     │────▶│  Gemini 2.5     │
│   Frontend   │◀────│   Backend     │◀────│  Flash Vision   │
└─────────────┘     └──────┬───────┘     └─────────────────┘
                           │
                    ┌──────▼───────┐
                    │  Playwright   │
                    │  (Chromium)   │
                    └──────────────┘
```

**Agent Loop:** User describes task → Screenshot → Gemini analyzes UI → Agent decides action → Playwright executes → New screenshot → Repeat until done

## Features

- **Pure visual navigation** — understands any website through screenshots
- **Multi-step reasoning** — plans and executes complex workflows
- **Human-in-the-loop** — asks for confirmation before sensitive actions (form submission, purchases)
- **Live screenshot stream** — watch the agent work in real-time
- **Step-by-step reasoning** — see the agent's thought process at each step

## Tech Stack

- **AI:** Gemini 2.5 Flash (vision/multimodal)
- **SDK:** Google GenAI SDK (`google-genai`)
- **Backend:** FastAPI + Uvicorn
- **Browser Automation:** Playwright + Chromium
- **Infrastructure:** Google Cloud Run + Terraform
- **Frontend:** Vanilla JS with live screenshot updates

## Quick Start

### Prerequisites
- Python 3.12+
- Gemini API key ([Get one here](https://aistudio.google.com/app/apikey))

### Local Development

```bash
# Clone
git clone https://github.com/eugeneleychenko/gemini-operator.git
cd gemini-operator

# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Set API key
export GEMINI_API_KEY=your-key-here

# Run
cd src && python server.py
# → http://localhost:8080
```

### Docker

```bash
docker build -t gemini-operator .
docker run -p 8080:8080 -e GEMINI_API_KEY=your-key gemini-operator
```

### Deploy to Google Cloud Run

```bash
cd terraform
terraform init
terraform apply -var="project_id=YOUR_PROJECT" -var="gemini_api_key=YOUR_KEY"
```

## How It Works

1. **User** enters a task description (e.g., "Search Google for cheap flights to Denver")
2. **Agent** navigates to the starting URL and takes a screenshot
3. **Gemini Vision** analyzes the screenshot, identifies UI elements and their coordinates
4. **Agent** reasons about the next action needed to complete the task
5. **Playwright** executes the action (click, type, scroll)
6. **Loop** repeats until task is complete or agent needs human confirmation
7. **Human** can approve or reject sensitive actions

## Project Structure

```
gemini-operator/
├── src/
│   ├── server.py           # FastAPI backend + API routes
│   ├── gemini_vision.py    # Gemini vision client (screenshot → analysis)
│   ├── browser.py          # Playwright browser controller
│   ├── agent.py            # Agent loop: observe → reason → act → verify
│   ├── actions.py          # Action executor (click, type, scroll)
│   └── models.py           # Pydantic models for tasks/actions
├── frontend/
│   ├── index.html          # Main UI
│   ├── app.js              # Frontend logic
│   └── styles.css          # Styles
├── terraform/
│   └── main.tf             # Cloud Run + Artifact Registry + Secret Manager
├── Dockerfile
├── requirements.txt
└── README.md
```

## Configuration

| Environment Variable | Description | Default |
|---------------------|-------------|---------|
| `GEMINI_API_KEY` | Google Gemini API key | Required |
| `PORT` | Server port | `8080` |
| `MAX_STEPS` | Max agent steps per task | `20` |
| `SCREENSHOT_DIR` | Screenshot storage path | `./screenshots` |

## Created for the #GeminiLiveAgentChallenge hackathon

Built with Google Gemini and Google Cloud.
