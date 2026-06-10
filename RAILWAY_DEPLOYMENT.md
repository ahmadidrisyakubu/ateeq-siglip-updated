# Railway Deployment Guide

This project is configured to be deployed on Railway. It automatically downloads the model from Hugging Face during startup, so you don't need to upload the heavy model files to GitHub.

## Steps to Deploy

1.  **Create a GitHub Repository**: Create a new private or public repository on GitHub.
2.  **Push the Code**:
    ```bash
    git init
    git add .
    git commit -m "Initial commit"
    git branch -M main
    git remote add origin <your-github-repo-url>
    git push -u origin main
    ```
3.  **Deploy on Railway**:
    *   Go to [Railway](https://railway.app/).
    *   Click **New Project** > **Deploy from GitHub repo**.
    *   Select your repository.
    *   Railway will automatically detect the `Procfile` and start the deployment.

## Important Notes

*   **Model Download**: The first time the app starts, it will download the model from Hugging Face (`waleeyd/deepfake-detector-image`). This might take a few minutes.
*   **Memory**: Ensure your Railway service has enough RAM (at least 2GB is recommended for this model).
*   **Storage**: The `uploads/` folder is used for temporary image processing and is cleared after each prediction.
*   **Labels**: The app is configured to output **Fake** for AI-generated images and **Real** for real images.

## Detection History Database

Add a Railway PostgreSQL service to the project and expose its `DATABASE_URL` variable to this application service. The app creates its `detections` table automatically during startup.

Without `DATABASE_URL`, the app uses `instance/detections.db` for local development. Railway's filesystem is ephemeral, so SQLite should not be used for production history unless a persistent volume is mounted.

No changes to the existing Procfile or start command are required.

New detections store a report-optimized image copy in PostgreSQL so downloaded PDF reports can include the detected image. Existing records created before this feature remain available, but their reports cannot display the original image.
