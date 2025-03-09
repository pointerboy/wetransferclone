# File Transfer Service

A modern, fast, and secure file transfer service built with FastAPI and Backblaze B2. This service allows users to upload files and share them with others through unique links. Files are automatically deleted after 14 days.

## Features

- Modern, responsive UI with drag-and-drop file upload
- Real-time upload progress with speed and ETA
- Automatic file expiry after 14 days
- Secure file storage using Backblaze B2
- Background image rotation
- Copy-to-clipboard functionality
- Multi-file upload support
- Download all files functionality

## Requirements

- Python 3.8+
- Backblaze B2 account with API credentials
- 100GB storage space
- 2GB RAM (minimum)

## Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd wetransferclone
```

2. Create a virtual environment and activate it:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure Backblaze B2:
   - Create a Backblaze B2 account
   - Create a bucket
   - Generate API credentials
   - Update the following variables in `app.py`:
     - `B2_APPLICATION_KEY_ID`
     - `B2_APPLICATION_KEY`
     - `B2_BUCKET_NAME`

5. Configure storage:
   - Update `STORAGE_BASE_DIR` in `app.py` to point to your desired storage location
   - Ensure the directory has appropriate permissions

## Running the Service

1. Start the server:
```bash
uvicorn app:app --host 0.0.0.0 --port 80
```

2. Access the service:
   - Open a web browser and navigate to `http://localhost:80`
   - For production, configure a proper web server (nginx, etc.) and use HTTPS

## Project Structure

```
wetransferclone/
├── app.py              # Main application file
├── requirements.txt    # Python dependencies
├── static/            # Static assets
│   ├── css/          # Stylesheets
│   │   └── styles.css
│   └── js/           # JavaScript files
│       ├── upload.js
│       └── download.js
├── templates/         # HTML templates
│   ├── base.html     # Base template
│   ├── upload.html   # Upload page
│   ├── download.html # Download page
│   └── error.html    # Error page
└── README.md         # This file
```

## Security Considerations

- Files are stored securely in Backblaze B2
- Automatic file cleanup after expiry
- Rate limiting and storage quotas
- No direct file access, all downloads go through the application
- Secure file naming and path handling

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

© 2023 Luka Trbović. All rights reserved.
