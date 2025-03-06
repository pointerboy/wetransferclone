from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import os, json, uuid, time, datetime, shutil
from pathlib import Path
from typing import Dict, Any
from b2sdk.v2 import B2Api, InMemoryAccountInfo

# B2 Configuration
B2_APPLICATION_KEY_ID = "bec925575d01"
B2_APPLICATION_KEY = "0036d7b3f5dfb4423881abfaaca8d4162e3ae570e1"
B2_BUCKET_NAME = "fdmbucket"

# File configuration
FILES_DB = "files.json"
FILE_EXPIRY_DAYS = 7

# Initialize B2 client
info = InMemoryAccountInfo()
b2_api = B2Api(info)
b2_api.authorize_account("production", B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY)
bucket = b2_api.get_bucket_by_name(B2_BUCKET_NAME)

# Initialize files db if it doesn't exist
if not os.path.exists(FILES_DB):
    with open(FILES_DB, "w") as f:
        json.dump({}, f)

def save_file_metadata(unique_id: str, file_url: str, filename: str, file_path: str, file_size: int) -> None:
    """Save file metadata to the JSON database with expiry date"""
    expiry_date = int(time.time() + (FILE_EXPIRY_DAYS * 24 * 60 * 60))  # Current time + 7 days in seconds
    
    with open(FILES_DB, "r+") as f:
        try:
            files = json.load(f) if os.path.getsize(FILES_DB) > 0 else {}
        except json.JSONDecodeError:
            files = {}
            
        files[unique_id] = {
            "url": file_url,
            "filename": filename,
            "file_path": file_path,
            "size": file_size,
            "upload_date": int(time.time()),
            "expiry_date": expiry_date
        }
        
        f.seek(0)
        f.truncate(0)
        json.dump(files, f, indent=2)

def get_file_metadata(file_id: str) -> Dict[str, Any]:
    """Get file metadata from the JSON database"""
    if not os.path.exists(FILES_DB):
        return None
        
    with open(FILES_DB, "r") as f:
        try:
            files = json.load(f)
            return files.get(file_id)
        except:
            return None

def cleanup_expired_files(background_tasks: BackgroundTasks) -> None:
    """Queue the cleanup task to run in the background"""
    background_tasks.add_task(_delete_expired_files)

def _delete_expired_files() -> None:
    """Delete expired files from B2 and update the database"""
    current_time = int(time.time())
    
    if not os.path.exists(FILES_DB):
        return
        
    with open(FILES_DB, "r") as f:
        try:
            files = json.load(f)
        except:
            return
    
    files_to_delete = []
    for file_id, file_data in files.items():
        if file_data.get("expiry_date", 0) < current_time:
            files_to_delete.append((file_id, file_data))
    
    if not files_to_delete:
        return
        
    # Delete expired files and update the database
    for file_id, file_data in files_to_delete:
        try:
            # Get the file name from the stored path
            file_path = file_data.get("file_path")
            if file_path:
                # Delete from B2
                file_versions = bucket.list_file_versions(file_path)
                for file_version in file_versions:
                    bucket.delete_file_version(file_version.id_, file_version.file_name)
                    
            # Remove from our database
            del files[file_id]
        except Exception as e:
            print(f"Error deleting expired file {file_id}: {str(e)}")
    
    # Update the database
    with open(FILES_DB, "w") as f:
        json.dump(files, f, indent=2)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def upload_page(background_tasks: BackgroundTasks):
    # Clean up expired files in the background
    cleanup_expired_files(background_tasks)
    
    return """
    <html>
        <head>
            <title>File Uploader</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
            <style>
                * {
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                    font-family: 'Inter', sans-serif;
                }
                
                body {
                    background-color: #f5f5f7;
                    color: #1d1d1f;
                    min-height: 100vh;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    padding: 20px;
                }
                
                .container {
                    background: white;
                    border-radius: 12px;
                    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
                    width: 100%;
                    max-width: 500px;
                    padding: 32px;
                    position: relative;
                }
                
                h2 {
                    text-align: center;
                    font-weight: 600;
                    font-size: 1.5rem;
                    margin-bottom: 24px;
                    color: #1d1d1f;
                }
                
                .upload-area {
                    border: 2px dashed #ccc;
                    border-radius: 8px;
                    padding: 32px 24px;
                    margin-bottom: 24px;
                    text-align: center;
                    transition: all 0.2s;
                    cursor: pointer;
                }
                
                .upload-area:hover {
                    border-color: #0071e3;
                }
                
                .upload-icon {
                    font-size: 32px;
                    margin-bottom: 12px;
                    color: #86868b;
                }
                
                .upload-text {
                    color: #86868b;
                    font-size: 14px;
                }
                
                #fileInput {
                    display: none;
                }
                
                .file-selected {
                    margin: 12px 0;
                    color: #0071e3;
                    font-size: 14px;
                    font-weight: 500;
                    display: none;
                    word-break: break-all;
                }
                
                .upload-btn {
                    background-color: #0071e3;
                    color: white;
                    border: none;
                    padding: 12px 0;
                    border-radius: 8px;
                    font-size: 0.9rem;
                    font-weight: 500;
                    cursor: pointer;
                    transition: all 0.2s;
                    width: 100%;
                    margin-bottom: 16px;
                }
                
                .upload-btn:hover {
                    background-color: #0077ed;
                }
                
                .upload-btn:disabled {
                    background-color: #86868b;
                    cursor: not-allowed;
                }
                
                .loader-container {
                    display: none;
                    margin: 24px 0;
                    text-align: center;
                }
                
                .progress-bar {
                    height: 6px;
                    width: 100%;
                    background-color: #e5e5e5;
                    border-radius: 3px;
                    overflow: hidden;
                    margin-bottom: 12px;
                }
                
                .progress-fill {
                    height: 100%;
                    width: 0%;
                    background-color: #0071e3;
                    transition: width 0.2s;
                }
                
                .percent {
                    font-size: 14px;
                    font-weight: 500;
                    color: #86868b;
                }
                
                .status {
                    margin-top: 16px;
                    font-weight: 500;
                    text-align: center;
                    min-height: 24px;
                    color: #86868b;
                }
                
                .link-container {
                    margin-top: 20px;
                    padding: 16px;
                    background-color: #f5f5f7;
                    border-radius: 8px;
                    display: none;
                    position: relative;
                }
                
                .copy-button {
                    position: absolute;
                    top: 16px;
                    right: 16px;
                    background: none;
                    border: none;
                    color: #0071e3;
                    cursor: pointer;
                    font-size: 16px;
                }
                
                .link-text {
                    margin-bottom: 10px;
                    color: #1d1d1f;
                }
                
                .link-url {
                    color: #0071e3;
                    word-break: break-all;
                    font-size: 14px;
                    padding: 8px;
                    background: rgba(0, 113, 227, 0.1);
                    border-radius: 4px;
                }
                
                .copied {
                    position: fixed;
                    top: 20px;
                    right: 20px;
                    background: rgba(0, 0, 0, 0.8);
                    color: white;
                    padding: 8px 16px;
                    border-radius: 4px;
                    font-size: 14px;
                    opacity: 0;
                    transition: opacity 0.3s;
                    z-index: 100;
                }
                
                .show-copied {
                    opacity: 1;
                }
                
                .expire-notice {
                    margin-top: 16px;
                    font-size: 12px;
                    color: #86868b;
                    text-align: center;
                }
                
                .donate-link {
                    display: block;
                    text-align: center;
                    margin-top: 24px;
                    color: #0071e3;
                    text-decoration: none;
                    font-size: 14px;
                    font-weight: 500;
                }
                
                .donate-link:hover {
                    text-decoration: underline;
                }
                
                @media (max-width: 600px) {
                    .container {
                        padding: 24px 16px;
                    }
                    
                    h2 {
                        font-size: 1.3rem;
                    }
                }
            </style>
            <script>
                document.addEventListener("DOMContentLoaded", function () {
                    const uploadArea = document.getElementById("uploadArea");
                    const fileInput = document.getElementById("fileInput");
                    const fileSelected = document.getElementById("fileSelected");
                    const uploadButton = document.getElementById("uploadButton");
                    const loaderContainer = document.getElementById("loaderContainer");
                    const progressFill = document.getElementById("progressFill");
                    const percentText = document.getElementById("percentText");
                    const statusText = document.getElementById("status");
                    const linkContainer = document.getElementById("linkContainer");
                    const copyButton = document.getElementById("copyButton");
                    const linkUrl = document.getElementById("linkUrl");
                    const copiedNotification = document.getElementById("copiedNotification");

                    // Handle drag & drop
                    ['dragover', 'dragenter'].forEach(eventName => {
                        uploadArea.addEventListener(eventName, (e) => {
                            e.preventDefault();
                            uploadArea.style.borderColor = "#0071e3";
                        });
                    });

                    ['dragleave', 'dragend'].forEach(eventName => {
                        uploadArea.addEventListener(eventName, () => {
                            uploadArea.style.borderColor = "#ccc";
                        });
                    });

                    uploadArea.addEventListener('drop', (e) => {
                        e.preventDefault();
                        uploadArea.style.borderColor = "#ccc";
                        if (e.dataTransfer.files.length) {
                            fileInput.files = e.dataTransfer.files;
                            updateFileLabel();
                        }
                    });

                    uploadArea.addEventListener('click', () => {
                        fileInput.click();
                    });

                    fileInput.addEventListener('change', updateFileLabel);

                    function updateFileLabel() {
                        if (fileInput.files.length > 0) {
                            fileSelected.textContent = `Selected: ${fileInput.files[0].name}`;
                            fileSelected.style.display = 'block';
                            uploadButton.disabled = false;
                        } else {
                            fileSelected.style.display = 'none';
                            uploadButton.disabled = true;
                        }
                    }
                    
                    // Copy to clipboard functionality
                    copyButton.addEventListener('click', () => {
                        navigator.clipboard.writeText(linkUrl.textContent)
                            .then(() => {
                                copiedNotification.classList.add('show-copied');
                                setTimeout(() => {
                                    copiedNotification.classList.remove('show-copied');
                                }, 2000);
                            });
                    });

                    uploadButton.addEventListener("click", async function () {
                        const file = fileInput.files[0];
                        if (!file) {
                            statusText.innerText = "Please select a file to upload";
                            statusText.style.color = "#ff3b30";
                            return;
                        }

                        // Reset UI
                        statusText.innerText = "Preparing upload...";
                        statusText.style.color = "#86868b";
                        linkContainer.style.display = "none";
                        uploadButton.disabled = true;
                        loaderContainer.style.display = "block";
                        progressFill.style.width = "0%";
                        percentText.textContent = "0%";

                        const formData = new FormData();
                        formData.append("file", file);

                        const xhr = new XMLHttpRequest();
                        xhr.open("POST", "/upload", true);

                        // Progress tracking
                        xhr.upload.addEventListener("progress", function (event) {
                            if (event.lengthComputable) {
                                const percent = Math.round((event.loaded / event.total) * 100);
                                progressFill.style.width = `${percent}%`;
                                percentText.textContent = `${percent}%`;
                            }
                        });

                        xhr.onload = function () {
                            loaderContainer.style.display = "none";
                            uploadButton.disabled = false;
                            
                            if (xhr.status === 200) {
                                const response = JSON.parse(xhr.responseText);
                                statusText.innerText = "Upload successful!";
                                statusText.style.color = "#34c759";
                                
                                // Display link and copy button
                                const fullUrl = window.location.origin + "/file/" + response.download_id;
                                linkUrl.textContent = fullUrl;
                                linkContainer.style.display = "block";
                                
                                // Reset file input
                                fileInput.value = "";
                                fileSelected.style.display = 'none';
                            } else {
                                statusText.innerText = "Error uploading file.";
                                statusText.style.color = "#ff3b30";
                            }
                        };

                        xhr.onerror = function () {
                            loaderContainer.style.display = "none";
                            uploadButton.disabled = false;
                            statusText.innerText = "Error uploading file.";
                            statusText.style.color = "#ff3b30";
                        };

                        xhr.send(formData);
                    });
                });
            </script>
        </head>
        <body>
            <div class="container">
                <h2>File Uploader</h2>
                
                <div id="uploadArea" class="upload-area">
                    <div class="upload-icon">📁</div>
                    <p class="upload-text">Drag & drop your file here<br>or click to browse</p>
                </div>
                
                <input type="file" id="fileInput"> 
                <p id="fileSelected" class="file-selected"></p>
                
                <button id="uploadButton" class="upload-btn" disabled>Upload</button>
                
                <div id="loaderContainer" class="loader-container">
                    <div class="progress-bar">
                        <div id="progressFill" class="progress-fill"></div>
                    </div>
                    <div id="percentText" class="percent">0%</div>
                </div>
                
                <p id="status" class="status"></p>
                
                <div id="linkContainer" class="link-container">
                    <p class="link-text">Your file is ready! Here's your link:</p>
                    <p id="linkUrl" class="link-url"></p>
                    <button id="copyButton" class="copy-button">📋</button>
                    <p class="expire-notice">This file will be available for 7 days</p>
                </div>
                
                <a href="/donate" class="donate-link">Support this project</a>
            </div>
            
            <div id="copiedNotification" class="copied">Link copied to clipboard!</div>
        </body>
    </html>
    """

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    if not file: 
        raise HTTPException(status_code=400, detail="No file provided")
    
    try:
        file_content = await file.read()
        file_size = len(file_content)
        
        if file_size > 50 * 1024 * 1024 * 1024:  # 50 GB limit
            raise HTTPException(status_code=400, detail="File size exceeds 50GB limit")
        
        # Generate a unique folder for this file
        unique_folder = str(uuid.uuid4())
        file_path = f"{unique_folder}/{file.filename}"
        
        # Upload to B2
        b2_file = bucket.upload_bytes(
            file_content, 
            file_path, 
            content_type=file.content_type or "application/octet-stream"
        )
        
        # Generate download URL
        file_url = f"https://f003.backblazeb2.com/file/{B2_BUCKET_NAME}/{file_path}"
        
        # Generate unique ID for download and save metadata
        unique_id = str(uuid.uuid4())[:8]
        save_file_metadata(unique_id, file_url, file.filename, file_path, file_size)
        
        return JSONResponse(content={
            "message": "Upload successful", 
            "file_url": file_url, 
            "download_id": unique_id
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@app.get("/file/{file_id}")
async def file_page(file_id: str, background_tasks: BackgroundTasks):
    # Clean up expired files in the background
    cleanup_expired_files(background_tasks)
    
    file_data = get_file_metadata(file_id)
    
    if not file_data:
        return HTMLResponse(content="""
        <html><head><title>File Not Found</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
        <style>
            body {
                font-family: 'Inter', sans-serif;
                background-color: #f5f5f7;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                color: #1d1d1f;
                text-align: center;
                margin: 0;
                padding: 20px;
            }
            .container {
                background: white;
                padding: 2rem;
                border-radius: 12px;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
                max-width: 500px;
                width: 100%;
            }
            h1 {
                margin-top: 0;
                font-size: 1.5rem;
            }
            a {
                color: #0071e3;
                text-decoration: none;
                border: 1px solid #0071e3;
                padding: 10px 20px;
                border-radius: 8px;
                display: inline-block;
                margin-top: 20px;
                transition: all 0.2s;
            }
            a:hover {
                background: #0071e3;
                color: white;
            }
        </style>
        </head>
        <body>
            <div class="container">
                <h1>File Not Found</h1>
                <p>Sorry, the file you're looking for doesn't exist or has been removed.</p>
                <a href="/">Return to Upload Page</a>
            </div>
        </body>
        </html>
        """, status_code=404)

    filename = file_data.get("filename", "file")
    file_url = file_data.get("url")
    file_size = file_data.get("size", 0)
    upload_date = file_data.get("upload_date", 0)
    expiry_date = file_data.get("expiry_date", 0)
    
    # Format sizes
    def format_size(size_in_bytes):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_in_bytes < 1024.0 or unit == 'GB':
                return f"{size_in_bytes:.2f} {unit}"
            size_in_bytes /= 1024.0
    
    # Format dates
    def format_date(timestamp):
        return datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')
    
    formatted_size = format_size(file_size)
    formatted_upload = format_date(upload_date) if upload_date else "Unknown"
    formatted_expiry = format_date(expiry_date) if expiry_date else "Unknown"
    
    days_left = max(0, int((expiry_date - time.time()) / (24 * 60 * 60))) if expiry_date else 0
    
    html_content = f"""<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Download {filename}</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
                font-family: 'Inter', sans-serif;
            }}
            
            body {{
                background-color: #f5f5f7;
                color: #1d1d1f;
                min-height: 100vh;
                display: flex;
                justify-content: center;
                align-items: center;
                padding: 20px;
            }}
            
            .container {{
                background: white;
                border-radius: 12px;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
                width: 100%;
                max-width: 500px;
                padding: 32px;
            }}
            
            h1 {{
                text-align: center;
                font-weight: 600;
                font-size: 1.5rem;
                margin-bottom: 24px;
                color: #1d1d1f;
            }}
            
            .file-info {{
                margin: 24px 0;
                padding: 16px;
                background-color: #f5f5f7;
                border-radius: 8px;
            }}
            
            .file-name {{
                font-weight: 500;
                color: #1d1d1f;
                font-size: 1rem;
                margin-bottom: 16px;
                word-break: break-all;
                text-align: center;
            }}
            
            .file-meta {{
                display: flex;
                flex-direction: column;
                gap: 8px;
                margin-bottom: 20px;
                font-size: 14px;
                color: #86868b;
            }}
            
            .file-meta-item {{
                display: flex;
                justify-content: space-between;
            }}
            
            .download-button {{
                display: block;
                width: 100%;
                background-color: #0071e3;
                color: white;
                text-align: center;
                text-decoration: none;
                padding: 12px;
                border-radius: 8px;
                font-weight: 500;
                transition: all 0.2s;
                margin-top: 16px;
            }}
            
            .download-button:hover {{
                background-color: #0077ed;
            }}
            
            .expire-notice {{
                margin-top: 16px;
                font-size: 13px;
                color: {{"#ff3b30" if days_left <= 1 else "#86868b"}};
                text-align: center;
            }}
            
            .back-link {{
                margin-top: 24px;
                display: block;
                text-align: center;
                color: #0071e3;
                text-decoration: none;
                font-size: 14px;
            }}
            
            .back-link:hover {{
                text-decoration: underline;
            }}
            
            .donate-link {{
                display: block;
                text-align: center;
                margin-top: 16px;
                color: #0071e3;
                text-decoration: none;
                font-size: 14px;
                font-weight: 500;
            }}
            
            .donate-link:hover {{
                text-decoration: underline;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Your File is Ready</h1>
            
            <div class="file-info">
                <div class="file-name">{filename}</div>
                
                <div class="file-meta">
                    <div class="file-meta-item">
                        <span>Size:</span>
                        <span>{formatted_size}</span>
                    </div>
                    <div class="file-meta-item">
                        <span>Uploaded:</span>
                        <span>{formatted_upload}</span>
                    </div>
                    <div class="file-meta-item">
                        <span>Expires:</span>
                        <span>{formatted_expiry}</span>
                    </div>
                </div>
                
                <a href="/download/{file_id}" class="download-button">Download Now</a>
            </div>
            
            <p class="expire-notice">This file will expire in {days_left} day{"s" if days_left != 1 else ""}</p>
            
            <a href="/" class="back-link">Upload another file</a>
            <a href="/donate" class="donate-link">Support this project</a>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/download/{file_id}")
async def download_file(file_id: str, background_tasks: BackgroundTasks):
    # Clean up expired files in the background
    cleanup_expired_files(background_tasks)
    
    file_data = get_file_metadata(file_id)
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")
    
    file_url = file_data.get("url")
    filename = file_data.get("filename")
    
    import httpx
    
    from fastapi.responses import StreamingResponse
    
    # Use httpx to fetch the file from the remote URL
    async with httpx.AsyncClient() as client:
        response = await client.get(file_url)
    
    if response.status_code != 200:
        raise HTTPException(status_code=404, detail="Failed to fetch file from remote server")
    
    # Return the file as a downloadable response
    return StreamingResponse(
        response.iter_bytes(), 
        media_type="application/octet-stream", 
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)