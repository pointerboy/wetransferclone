from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import os, json, uuid, time, datetime, shutil, math, re, tempfile, subprocess, asyncio, platform, zipfile
from pathlib import Path
from typing import Dict, Any
from b2sdk.v2 import B2Api, InMemoryAccountInfo
import httpx
import mimetypes

# B2 Configuration
B2_APPLICATION_KEY_ID = "bec925575d01"
B2_APPLICATION_KEY = "0036d7b3f5dfb4423881abfaaca8d4162e3ae570e1"
B2_BUCKET_NAME = "fdmbucket"

# File configuration
FILES_DB = "files.json"
FILE_EXPIRY_DAYS = 7

# Storage configuration
STORAGE_BASE_DIR = "/mnt/disk"  # Base directory for all storage operations
TEMP_UPLOAD_DIR = os.path.join(STORAGE_BASE_DIR, "temp_uploads")  # Temporary upload directory
TOOLS_DIR = os.path.join(STORAGE_BASE_DIR, "tools")  # Tools directory
MAX_TEMP_STORAGE = 90 * 1024 * 1024 * 1024  # 90GB max temp storage (leaving some buffer for system)
CHUNK_SIZE = 2 * 1024 * 1024  # 2MB chunks for file operations (reduced for better memory management)
MAX_CONCURRENT_UPLOADS = 4  # Limit concurrent uploads to prevent memory issues
CACHE_EXPIRY = 24 * 60 * 60  # 24 hours cache expiry

# Ensure directories exist with proper permissions
for directory in [STORAGE_BASE_DIR, TEMP_UPLOAD_DIR, TOOLS_DIR]:
    try:
        os.makedirs(directory, exist_ok=True)
        # Set directory permissions to 755 (rwxr-xr-x)
        os.chmod(directory, 0o755)
    except Exception as e:
        print(f"Error creating directory {directory}: {str(e)}")
        raise

# Initialize files db if it doesn't exist
if not os.path.exists(FILES_DB):
    with open(FILES_DB, "w") as f:
        json.dump({}, f)

def get_temp_storage_usage():
    """Get current usage of temporary storage"""
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(TEMP_UPLOAD_DIR):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
    return total_size

def cleanup_temp_storage():
    """Clean up temporary storage if it exceeds the limit"""
    current_usage = get_temp_storage_usage()
    if current_usage > MAX_TEMP_STORAGE:
        print(f"Temporary storage usage ({current_usage / (1024**3):.2f}GB) exceeds limit. Cleaning up...")
        # Delete files older than cache expiry
        current_time = time.time()
        for dirpath, dirnames, filenames in os.walk(TEMP_UPLOAD_DIR):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if current_time - os.path.getmtime(fp) > CACHE_EXPIRY:
                    try:
                        os.remove(fp)
                        print(f"Deleted old temporary file: {fp}")
                    except Exception as e:
                        print(f"Error deleting temporary file {fp}: {str(e)}")

# Function to ensure rclone is available
def ensure_rclone():
    """Ensure rclone is available, downloading it if necessary"""
    # Define rclone paths
    rclone_dir = TOOLS_DIR
    
    # Determine platform-specific executable name
    system = platform.system().lower()
    if system == "windows":
        rclone_exe = os.path.join(rclone_dir, "rclone.exe")
    else:
        rclone_exe = os.path.join(rclone_dir, "rclone")
    
    # Check if rclone already exists
    if os.path.exists(rclone_exe):
        return rclone_exe
    
    print("Rclone not found. Downloading...")
    
    # Determine download URL based on platform
    if system == "windows":
        download_url = "https://downloads.rclone.org/rclone-current-windows-amd64.zip"
        zip_path = os.path.join(rclone_dir, "rclone.zip")
    elif system == "darwin":  # macOS
        download_url = "https://downloads.rclone.org/rclone-current-osx-amd64.zip"
        zip_path = os.path.join(rclone_dir, "rclone.zip")
    else:  # Linux and others
        download_url = "https://downloads.rclone.org/rclone-current-linux-amd64.zip"
        zip_path = os.path.join(rclone_dir, "rclone.zip")
    
    # Download rclone
    try:
        with httpx.Client() as client:
            response = client.get(download_url)
            response.raise_for_status()
            
            with open(zip_path, 'wb') as f:
                f.write(response.content)
        
        # Extract rclone
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(rclone_dir)
        
        # Find the extracted rclone executable
        extracted_dir = None
        for item in os.listdir(rclone_dir):
            if item.startswith("rclone-") and os.path.isdir(os.path.join(rclone_dir, item)):
                extracted_dir = os.path.join(rclone_dir, item)
                break
        
        if not extracted_dir:
            raise Exception("Could not find extracted rclone directory")
        
        # Move the rclone executable to the tools directory
        if system == "windows":
            src_exe = os.path.join(extracted_dir, "rclone.exe")
        else:
            src_exe = os.path.join(extracted_dir, "rclone")
        
        shutil.copy2(src_exe, rclone_exe)
        
        # Make executable on Unix-like systems
        if system != "windows":
            os.chmod(rclone_exe, 0o755)
        
        # Clean up
        os.remove(zip_path)
        shutil.rmtree(extracted_dir)
        
        print(f"Rclone downloaded and installed at {rclone_exe}")
        return rclone_exe
        
    except Exception as e:
        print(f"Error downloading rclone: {str(e)}")
        raise Exception(f"Failed to download rclone: {str(e)}")

# Initialize B2 client
info = InMemoryAccountInfo()
b2_api = B2Api(info)
b2_api.authorize_account("production", B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY)
bucket = b2_api.get_bucket_by_name(B2_BUCKET_NAME)

def save_file_metadata(unique_id: str, files_data: list) -> None:
    """Save file metadata to the JSON database with expiry date"""
    expiry_date = int(time.time() + (FILE_EXPIRY_DAYS * 24 * 60 * 60))  # Current time + 7 days in seconds
    
    with open(FILES_DB, "r+") as f:
        try:
            files = json.load(f) if os.path.getsize(FILES_DB) > 0 else {}
        except json.JSONDecodeError:
            files = {}
            
        files[unique_id] = {
            "files": files_data,
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
    """Queue the cleanup tasks to run in the background"""
    background_tasks.add_task(_delete_expired_files)
    background_tasks.add_task(cleanup_temp_storage)

async def _delete_expired_files() -> None:
    """Delete expired files from B2 and update the database"""
    current_time = int(time.time())
    
    if not os.path.exists(FILES_DB):
        return
        
    try:
        with open(FILES_DB, "r") as f:
            files = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error reading files database: {str(e)}")
        return
    
    files_to_delete = []
    for file_id, file_data in files.items():
        if file_data.get("expiry_date", 0) < current_time:
            files_to_delete.append((file_id, file_data))
    
    if not files_to_delete:
        return
        
    # Ensure rclone is available
    try:
        rclone_path = ensure_rclone()
    except Exception as e:
        print(f"Error ensuring rclone is available: {str(e)}")
        return
        
    # Delete expired files and update the database
    for file_id, file_data in files_to_delete:
        try:
            # Delete all files from B2
            for file_info in file_data.get("files", []):
                file_path = file_info.get("file_path")
                if file_path:
                    try:
                        # Use rclone to delete the file
                        subprocess.run(
                            [
                                rclone_path,
                                "--config", os.path.join(os.getcwd(), "rclone.conf"),
                                "delete", 
                                f"b2:{B2_BUCKET_NAME}/{file_path}"
                            ],
                            check=True,
                            capture_output=True
                        )
                    except subprocess.CalledProcessError as e:
                        print(f"Error deleting file {file_path}: {e.stderr.decode()}")
                    
            # Remove from our database
            del files[file_id]
        except Exception as e:
            print(f"Error deleting expired file {file_id}: {str(e)}")
    
    # Update the database
    try:
        with open(FILES_DB, "w") as f:
            json.dump(files, f, indent=2)
    except IOError as e:
        print(f"Error writing to files database: {str(e)}")

def generate_unique_folder() -> str:
    """Generate a unique folder name"""
    return str(uuid.uuid4())

def validate_b2_filename(filename: str) -> bool:
    try:
        # Check if filename is empty
        if not filename:
            raise ValueError("Filename cannot be empty")
            
        # Check UTF-8 encoding and length
        encoded_name = filename.encode('utf-8')
        if len(encoded_name) > 1024:
            raise ValueError("Filename too long (max 1024 bytes when UTF-8 encoded)")
            
        # Check for invalid patterns
        if filename.startswith('/'):
            raise ValueError("Filename cannot start with '/'")
        if filename.endswith('/'):
            raise ValueError("Filename cannot end with '/'")
        if '//' in filename:
            raise ValueError("Filename cannot contain '//'")
            
        # Check for control characters
        if any(ord(c) < 32 or ord(c) == 127 for c in filename):
            raise ValueError("Filename cannot contain control characters")
            
        # Check individual segments (parts between slashes) length
        segments = filename.split('/')
        for segment in segments:
            if len(segment.encode('utf-8')) > 250:
                raise ValueError("Individual filename segments cannot exceed 250 bytes when UTF-8 encoded")
                
        return True
    except Exception as e:
        raise ValueError(f"Invalid filename: {str(e)}")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def upload_page(background_tasks: BackgroundTasks):
    # Clean up expired files in the background
    cleanup_expired_files(background_tasks)
    
    return """
    <html>
        <head>
            <title>Transfer Fajlova</title>
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
                    overflow: hidden;
                }
                
                .split-layout {
                    display: flex;
                    width: 100%;
                    height: 100vh;
                }
                
                .upload-section {
                    flex: 1;
                    padding: 40px;
                    display: flex;
                    flex-direction: column;
                    justify-content: center;
                    position: relative;
                    z-index: 1;
                    background: white;
                    padding-bottom: 80px; /* Add padding to prevent content from being hidden behind copyright */
                }
                
                .background-section {
                    flex: 1;
                    position: relative;
                    overflow: hidden;
                    display: none;
                }
                
                .background-image {
                    position: absolute;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    object-fit: cover;
                    opacity: 0;
                    transition: opacity 0.5s ease;
                }
                
                .background-image.active {
                    opacity: 1;
                }
                
                .background-overlay {
                    position: absolute;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    background: linear-gradient(45deg, rgba(0,0,0,0.3), rgba(0,0,0,0.1));
                }
                
                .background-credit {
                    position: absolute;
                    bottom: 20px;
                    right: 20px;
                    color: white;
                    font-size: 12px;
                    text-shadow: 0 1px 2px rgba(0,0,0,0.3);
                    z-index: 2;
                }
                
                .container {
                    max-width: 500px;
                    margin: 0 auto;
                    width: 100%;
                }
                
                h2 {
                    font-weight: 600;
                    font-size: 2rem;
                    margin-bottom: 24px;
                    color: #1d1d1f;
                    line-height: 1.2;
                }
                
                .upload-area {
                    border: 2px dashed #ccc;
                    border-radius: 12px;
                    padding: 40px 24px;
                    margin-bottom: 24px;
                    text-align: center;
                    transition: all 0.3s ease;
                    cursor: pointer;
                    background: #fafafa;
                    position: relative;
                    overflow: hidden;
                }
                
                .upload-area::before {
                    content: '';
                    position: absolute;
                    top: 0;
                    left: 0;
                    right: 0;
                    bottom: 0;
                    background: linear-gradient(45deg, transparent, rgba(0, 113, 227, 0.1), transparent);
                    opacity: 0;
                    transition: opacity 0.3s ease;
                }
                
                .upload-area:hover::before {
                    opacity: 1;
                }
                
                .upload-area.dragover {
                    border-color: #0071e3;
                    background: #f0f0f2;
                    transform: scale(1.02);
                }
                
                .upload-area.dragover::before {
                    opacity: 1;
                }
                
                .upload-icon {
                    font-size: 48px;
                    margin-bottom: 16px;
                    color: #86868b;
                }
                
                .upload-text {
                    color: #86868b;
                    font-size: 16px;
                    line-height: 1.5;
                }
                
                #fileInput {
                    display: none;
                }
                
                .files-list {
                    margin: 16px 0;
                    display: none;
                    max-height: 300px;
                    overflow-y: auto;
                }
                
                .file-item {
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    padding: 12px 16px;
                    background-color: #f5f5f7;
                    border-radius: 8px;
                    margin-bottom: 8px;
                    font-size: 14px;
                    transition: all 0.2s ease;
                }
                
                .file-item:hover {
                    background-color: #e5e5e7;
                }
                
                .file-name {
                    flex: 1;
                    margin-right: 12px;
                    word-break: break-all;
                    color: #1d1d1f;
                }
                
                .file-size {
                    color: #86868b;
                    white-space: nowrap;
                }
                
                .remove-file {
                    color: #ff3b30;
                    cursor: pointer;
                    padding: 6px 10px;
                    margin-left: 8px;
                    border-radius: 6px;
                    transition: all 0.2s ease;
                }
                
                .remove-file:hover {
                    background-color: rgba(255, 59, 48, 0.1);
                }
                
                .upload-btn {
                    background-color: #0071e3;
                    color: white;
                    border: none;
                    padding: 14px 0;
                    border-radius: 8px;
                    font-size: 16px;
                    font-weight: 500;
                    cursor: pointer;
                    transition: all 0.2s ease;
                    width: 100%;
                    margin-bottom: 16px;
                    box-shadow: none;
                }
                
                .upload-btn:hover {
                    background-color: #0077ed;
                    transform: translateY(-1px);
                    box-shadow: 0 2px 4px rgba(0, 113, 227, 0.2);
                }
                
                .upload-btn:disabled {
                    background-color: #86868b;
                    cursor: not-allowed;
                    transform: none;
                    box-shadow: none;
                }
                
                .loader-container {
                    display: none;
                    margin: 24px 0;
                    text-align: center;
                    position: relative;
                    z-index: 2;
                }
                
                .progress-bar {
                    height: 8px;
                    width: 100%;
                    background-color: #e5e5e5;
                    border-radius: 4px;
                    overflow: hidden;
                    margin-bottom: 12px;
                    position: relative;
                }
                
                .progress-fill {
                    height: 100%;
                    width: 0%;
                    background-color: #0071e3;
                    transition: width 0.3s ease;
                    position: relative;
                }
                
                .progress-fill::after {
                    content: '';
                    position: absolute;
                    top: 0;
                    left: 0;
                    right: 0;
                    bottom: 0;
                    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.2), transparent);
                    animation: shimmer 1.5s infinite;
                }
                
                @keyframes shimmer {
                    0% {
                        transform: translateX(-100%);
                    }
                    100% {
                        transform: translateX(100%);
                    }
                }
                
                .progress-info {
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    margin-bottom: 8px;
                }
                
                .percent {
                    font-size: 14px;
                    font-weight: 500;
                    color: #86868b;
                }
                
                .upload-speed {
                    font-size: 14px;
                    color: #86868b;
                }
                
                .status {
                    margin-top: 16px;
                    font-weight: 500;
                    text-align: center;
                    min-height: 24px;
                    color: #86868b;
                    transition: all 0.3s ease;
                    position: relative;
                    z-index: 2;
                    padding: 8px;
                    border-radius: 4px;
                }
                
                .status.error {
                    color: #ff3b30;
                }
                
                .status.success {
                    color: #34c759;
                    background: white;
                    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
                }
                
                .link-container {
                    margin-top: 20px;
                    padding: 20px;
                    background-color: #f5f5f7;
                    border-radius: 12px;
                    display: none;
                    position: relative;
                }
                
                .copy-button {
                    position: absolute;
                    top: 20px;
                    right: 20px;
                    background: none;
                    border: none;
                    color: #0071e3;
                    cursor: pointer;
                    font-size: 20px;
                    padding: 8px;
                    border-radius: 6px;
                    transition: all 0.2s ease;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    width: 36px;
                    height: 36px;
                }
                
                .copy-button:hover {
                    background-color: rgba(0, 113, 227, 0.1);
                }
                
                .link-text {
                    margin-bottom: 12px;
                    color: #1d1d1f;
                    font-weight: 500;
                }
                
                .link-url {
                    color: #0071e3;
                    word-break: break-all;
                    font-size: 14px;
                    padding: 12px;
                    background: rgba(0, 113, 227, 0.1);
                    border-radius: 6px;
                    line-height: 1.5;
                }
                
                .copied {
                    position: fixed;
                    top: 20px;
                    right: 20px;
                    background: rgba(0, 0, 0, 0.8);
                    color: white;
                    padding: 12px 24px;
                    border-radius: 8px;
                    font-size: 14px;
                    opacity: 0;
                    transition: opacity 0.3s ease;
                    z-index: 100;
                    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
                }
                
                .show-copied {
                    opacity: 1;
                }
                
                .expire-notice {
                    margin-top: 16px;
                    font-size: 13px;
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
                    transition: all 0.2s ease;
                }
                
                .donate-link:hover {
                    text-decoration: underline;
                }
                
                .upload-states {
                    display: none;
                    margin-top: 16px;
                    text-align: center;
                    font-size: 14px;
                    color: #86868b;
                    min-height: 24px;
                    position: relative;
                    z-index: 2;
                }
                
                .upload-state {
                    opacity: 0;
                    transition: opacity 0.3s ease;
                    position: absolute;
                    width: 100%;
                    text-align: center;
                    background: white;
                    padding: 8px;
                    border-radius: 4px;
                    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
                }
                
                .upload-state.active {
                    opacity: 1;
                }
                
                .copyright {
                    position: fixed;
                    bottom: 20px;
                    left: 50%;
                    transform: translateX(-50%);
                    color: #86868b;
                    font-size: 12px;
                    text-align: center;
                    width: 100%;
                    padding: 10px 20px;
                    z-index: 1000;
                    background: rgba(255, 255, 255, 0.9);
                    backdrop-filter: blur(5px);
                    border-radius: 8px;
                    margin: 0 auto;
                    max-width: 500px;
                    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
                }
                
                @media (min-width: 1024px) {
                    .background-section {
                        display: block;
                    }
                }
                
                @media (max-width: 768px) {
                    .split-layout {
                        flex-direction: column;
                    }
                    
                    .upload-section {
                        padding: 24px 16px;
                    }
                    
                    h2 {
                        font-size: 1.75rem;
                    }
                    
                    .upload-area {
                        padding: 24px 16px;
                    }
                    
                    .upload-icon {
                        font-size: 36px;
                    }
                    
                    .container {
                        padding: 20px 16px;
                    }
                    
                    .file-item {
                        padding: 12px;
                    }
                    
                    .link-container {
                        padding: 16px;
                    }
                    
                    .copy-button {
                        top: 16px;
                        right: 16px;
                        width: 32px;
                        height: 32px;
                    }
                }
            </style>
            <script>
                document.addEventListener("DOMContentLoaded", function() {
                    // Get all required elements
                    const uploadArea = document.getElementById("uploadArea");
                    const fileInput = document.getElementById("fileInput");
                    const filesList = document.getElementById("filesList");
                    const uploadButton = document.getElementById("uploadButton");
                    const loaderContainer = document.getElementById("loaderContainer");
                    const progressFill = document.getElementById("progressFill");
                    const percentText = document.getElementById("percentText");
                    const uploadSpeed = document.getElementById("uploadSpeed");
                    const statusText = document.getElementById("status");
                    const linkContainer = document.getElementById("linkContainer");
                    const copyButton = document.getElementById("copyButton");
                    const linkUrl = document.getElementById("linkUrl");
                    const copiedNotification = document.getElementById("copiedNotification");
                    
                    // Initialize state variables
                    let selectedFiles = [];
                    let uploadStartTime = null;
                    let lastUploadedBytes = 0;
                    let lastSpeedUpdateTime = null;

                    // Utility functions
                    function formatBytes(bytes, decimals = 2) {
                        if (bytes === 0) return '0 Bytes';
                        const k = 1024;
                        const dm = decimals < 0 ? 0 : decimals;
                        const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
                        const i = Math.floor(Math.log(bytes) / Math.log(k));
                        return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
                    }

                    function formatSpeed(bytesPerSecond) {
                        return formatBytes(bytesPerSecond) + '/s';
                    }

                    function formatTime(seconds) {
                        if (seconds < 60) return `${Math.round(seconds)}s`;
                        if (seconds < 3600) {
                            const minutes = Math.floor(seconds / 60);
                            const secs = Math.round(seconds % 60);
                            return `${minutes}m ${secs}s`;
                        }
                        const hours = Math.floor(seconds / 3600);
                        const minutes = Math.floor((seconds % 3600) / 60);
                        return `${hours}h ${minutes}m`;
                    }

                    // File handling functions
                    function addFileToList(file) {
                        const fileItem = document.createElement('div');
                        fileItem.className = 'file-item';
                        
                        const fileName = document.createElement('span');
                        fileName.className = 'file-name';
                        fileName.textContent = file.name;
                        
                        const fileSize = document.createElement('span');
                        fileSize.className = 'file-size';
                        fileSize.textContent = formatBytes(file.size);
                        
                        const removeButton = document.createElement('span');
                        removeButton.className = 'remove-file';
                        removeButton.innerHTML = '×';
                        removeButton.onclick = () => {
                            selectedFiles = selectedFiles.filter(f => f !== file);
                            fileItem.remove();
                            updateUploadButton();
                        };
                        
                        fileItem.appendChild(fileName);
                        fileItem.appendChild(fileSize);
                        fileItem.appendChild(removeButton);
                        filesList.appendChild(fileItem);
                        filesList.style.display = 'block';
                    }

                    function handleFiles(files) {
                        Array.from(files).forEach(file => {
                            if (!selectedFiles.some(f => f.name === file.name)) {
                                selectedFiles.push(file);
                                addFileToList(file);
                            }
                        });
                        updateUploadButton();
                    }

                    function updateUploadButton() {
                        if (uploadButton) {
                            uploadButton.disabled = selectedFiles.length === 0;
                        }
                    }

                    function resetUploadUI() {
                        if (statusText) {
                            statusText.innerText = "Pripremam upload...";
                            statusText.style.color = "#86868b";
                        }
                        if (linkContainer) {
                            linkContainer.style.display = "none";
                        }
                        if (uploadButton) {
                            uploadButton.disabled = true;
                        }
                        if (loaderContainer) {
                            loaderContainer.style.display = "block";
                        }
                        if (progressFill) {
                            progressFill.style.width = "0%";
                        }
                        if (percentText) {
                            percentText.textContent = "0%";
                        }
                        if (uploadSpeed) {
                            uploadSpeed.textContent = "Računam...";
                        }
                        uploadStartTime = null;
                        lastUploadedBytes = 0;
                        lastSpeedUpdateTime = null;
                    }

                    // Event listeners
                    if (uploadArea) {
                        uploadArea.addEventListener('click', () => {
                            if (fileInput) {
                                fileInput.click();
                            }
                        });

                        ['dragover', 'dragenter'].forEach(eventName => {
                            uploadArea.addEventListener(eventName, (e) => {
                                e.preventDefault();
                                uploadArea.classList.add('dragover');
                            });
                        });

                        ['dragleave', 'dragend'].forEach(eventName => {
                            uploadArea.addEventListener(eventName, () => {
                                uploadArea.classList.remove('dragover');
                            });
                        });

                        uploadArea.addEventListener('drop', (e) => {
                            e.preventDefault();
                            uploadArea.classList.remove('dragover');
                            if (e.dataTransfer.files.length) {
                                handleFiles(e.dataTransfer.files);
                            }
                        });
                    }

                    if (fileInput) {
                        fileInput.addEventListener('change', (e) => {
                            if (e.target.files.length) {
                                handleFiles(e.target.files);
                            }
                        });
                    }

                    if (uploadButton) {
                        uploadButton.addEventListener("click", async function () {
                            if (selectedFiles.length === 0) {
                                if (statusText) {
                                    statusText.innerText = "Molimo izaberite fajlove za upload";
                                    statusText.style.color = "#ff3b30";
                                }
                                return;
                            }

                            resetUploadUI();

                            const formData = new FormData();
                            let totalSize = 0;
                            selectedFiles.forEach(file => {
                                formData.append("files", file);
                                totalSize += file.size;
                            });

                            try {
                                const xhr = new XMLHttpRequest();
                                xhr.open("POST", "/upload", true);

                                xhr.upload.addEventListener("progress", function (event) {
                                    if (event.lengthComputable) {
                                        const percent = Math.round((event.loaded / event.total) * 100);
                                        if (progressFill) {
                                            progressFill.style.width = `${percent}%`;
                                        }
                                        if (percentText) {
                                            percentText.textContent = `${percent}%`;
                                        }
                                        
                                        const now = Date.now();
                                        if (!uploadStartTime) {
                                            uploadStartTime = now;
                                            lastSpeedUpdateTime = now;
                                            lastUploadedBytes = 0;
                                        }
                                        
                                        const timeDiff = (now - lastSpeedUpdateTime) / 1000;
                                        if (timeDiff >= 1) {
                                            const bytesDiff = event.loaded - lastUploadedBytes;
                                            const speed = bytesDiff / timeDiff;
                                            const remainingBytes = event.total - event.loaded;
                                            const eta = remainingBytes / speed;
                                            
                                            const speedFormatted = formatSpeed(speed);
                                            const etaFormatted = formatTime(eta);
                                            
                                            if (uploadSpeed) {
                                                uploadSpeed.textContent = `${speedFormatted} • ${etaFormatted} preostalo`;
                                            }
                                            
                                            lastUploadedBytes = event.loaded;
                                            lastSpeedUpdateTime = now;
                                        }
                                    }
                                });

                                xhr.onload = function () {
                                    if (xhr.status === 200) {
                                        const response = JSON.parse(xhr.responseText);
                                        if (statusText) {
                                            statusText.innerText = "Upload uspešan!";
                                            statusText.style.color = "#34c759";
                                            statusText.classList.add('success');
                                        }
                                        
                                        if (linkUrl) {
                                            const fullUrl = window.location.origin + "/file/" + response.download_id;
                                            linkUrl.textContent = fullUrl;
                                        }
                                        
                                        if (linkContainer) {
                                            linkContainer.style.display = "block";
                                        }
                                        
                                        // Reset form
                                        if (fileInput) {
                                            fileInput.value = "";
                                        }
                                        selectedFiles = [];
                                        if (filesList) {
                                            filesList.innerHTML = "";
                                            filesList.style.display = "none";
                                        }
                                    } else {
                                        let errorMessage = 'Nepoznata greška';
                                        try {
                                            const response = JSON.parse(xhr.responseText);
                                            errorMessage = response.detail || response.message || errorMessage;
                                        } catch (e) {
                                            errorMessage = xhr.responseText || errorMessage;
                                        }
                                        if (statusText) {
                                            statusText.innerText = `Greška: ${errorMessage}`;
                                            statusText.style.color = "#ff3b30";
                                        }
                                    }
                                    
                                    if (loaderContainer) {
                                        loaderContainer.style.display = "none";
                                    }
                                    if (uploadButton) {
                                        uploadButton.disabled = false;
                                    }
                                };

                                xhr.onerror = function () {
                                    if (statusText) {
                                        statusText.innerText = "Greška pri uploadu fajlova.";
                                        statusText.style.color = "#ff3b30";
                                    }
                                    if (loaderContainer) {
                                        loaderContainer.style.display = "none";
                                    }
                                    if (uploadButton) {
                                        uploadButton.disabled = false;
                                    }
                                };

                                xhr.send(formData);
                            } catch (error) {
                                console.error('Upload error:', error);
                                if (statusText) {
                                    statusText.innerText = `Greška: ${error.message}`;
                                    statusText.style.color = "#ff3b30";
                                }
                                if (loaderContainer) {
                                    loaderContainer.style.display = "none";
                                }
                                if (uploadButton) {
                                    uploadButton.disabled = false;
                                }
                            }
                        });
                    }

                    if (copyButton && linkUrl) {
                        copyButton.addEventListener('click', () => {
                            navigator.clipboard.writeText(linkUrl.textContent)
                                .then(() => {
                                    if (copiedNotification) {
                                        copiedNotification.classList.add('show-copied');
                                        setTimeout(() => {
                                            copiedNotification.classList.remove('show-copied');
                                        }, 2000);
                                    }
                                });
                        });
                    }
                });
            </script>
        </head>
        <body>
            <div class="split-layout">
                <div class="upload-section">
                    <div class="container">
                        <h2>Podelite svoje fajlove<br>sa bilo kim, bilo gde</h2>
                        
                        <div id="uploadArea" class="upload-area">
                            <div class="upload-icon">📁</div>
                            <p class="upload-text">Prevucite fajlove ovde<br>ili kliknite za pretragu</p>
                        </div>
                        
                        <input type="file" id="fileInput" multiple> 
                        <div id="filesList" class="files-list"></div>
                        
                        <button id="uploadButton" class="upload-btn" disabled>Upload</button>
                        
                        <div id="loaderContainer" class="loader-container">
                            <div class="progress-info">
                                <div id="percentText" class="percent">0%</div>
                                <div id="uploadSpeed" class="upload-speed">Računam...</div>
                            </div>
                            <div class="progress-bar">
                                <div id="progressFill" class="progress-fill"></div>
                            </div>
                        </div>
                        
                        <p id="status" class="status"></p>
                        
                        <div id="linkContainer" class="link-container">
                            <p class="link-text">Vaši fajlovi su spremni! Evo vašeg linka:</p>
                            <p id="linkUrl" class="link-url"></p>
                            <button id="copyButton" class="copy-button">📋</button>
                            <p class="expire-notice">Ovi fajlovi će biti dostupni 7 dana</p>
                        </div>
                    </div>
                </div>
                
                <div class="background-section">
                    <div class="background-overlay"></div>
                    <div class="background-credit"></div>
                </div>
            </div>
            
            <div id="copiedNotification" class="copied">Link kopiran u clipboard!</div>
            <div class="copyright">© """ + str(datetime.datetime.now().year) + """ Luka Trbović. Sva prava zadržana.</div>
        </body>
    </html>
    """

@app.post("/upload")
async def upload_file(files: list[UploadFile] = File(...)):
    if not files: 
        raise HTTPException(status_code=400, detail="No files provided")
    
    try:
        # Ensure rclone is available
        rclone_path = ensure_rclone()
        unique_folder = generate_unique_folder()
        print(f"\n=== Starting new upload session ===")
        print(f"Number of files: {len(files)}")
        print(f"Generated unique folder: {unique_folder}")

        # Create rclone config file with proper B2 configuration
        rclone_config = os.path.join(os.getcwd(), "rclone.conf")
        with open(rclone_config, "w") as f:
            f.write(f"""[b2]
type = b2
account = {B2_APPLICATION_KEY_ID}
key = {B2_APPLICATION_KEY}
hard_delete = true
upload_cutoff = 100M
chunk_size = 50M
max_upload_parts = 50
max_upload_concurrency = 4
max_upload_speed = 0
max_download_speed = 0
buffer_size = 50M
memory_buffer_size = 50M
""")

        files_data = []
        upload_tasks = []
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)  # Limit concurrent uploads

        async def upload_single_file(file: UploadFile, file_path: str, content_type: str) -> dict:
            async with semaphore:  # Use semaphore to limit concurrent uploads
                try:
                    if not file.filename:
                        raise HTTPException(status_code=400, detail="File name is required")
                    
                    print(f"\n=== Processing file: {file.filename} ===")
                    print(f"File size: {file.size} bytes")
                    
                    # Validate filename
                    safe_filename = file.filename.encode('utf-8').decode('utf-8')
                    safe_filename = "".join(c for c in safe_filename if c.isprintable())
                    validate_b2_filename(safe_filename)
                    
                    # Create a unique temporary file path
                    temp_file_path = os.path.join(TEMP_UPLOAD_DIR, f"{uuid.uuid4()}_{safe_filename}")
                    
                    try:
                        print("Starting file read...")
                        # Read and write in chunks with timeout
                        total_size = 0
                        last_progress_time = time.time()
                        last_progress_size = 0
                        
                        # Open file in binary write mode
                        with open(temp_file_path, 'wb') as temp_file:
                            while True:
                                try:
                                    chunk = await asyncio.wait_for(file.read(CHUNK_SIZE), timeout=30.0)
                                    if not chunk:
                                        break
                                    temp_file.write(chunk)
                                    total_size += len(chunk)
                                    
                                    # Update progress every 2 seconds
                                    current_time = time.time()
                                    if current_time - last_progress_time >= 2:
                                        bytes_since_last = total_size - last_progress_size
                                        speed = bytes_since_last / (current_time - last_progress_time)
                                        print(f"Progress: {total_size} bytes written ({format(total_size/file.size*100, '.1f')}%)")
                                        print(f"Current speed: {format(speed/1024/1024, '.1f')} MB/s")
                                        last_progress_time = current_time
                                        last_progress_size = total_size
                                        
                                except asyncio.TimeoutError:
                                    print("Upload timeout - connection too slow")
                                    raise HTTPException(
                                        status_code=408,
                                        detail="Upload timeout - connection too slow"
                                    )
                        
                        print("File read complete, starting rclone upload...")
                        
                        try:
                            # Use rclone to copy the file to B2 with optimized settings
                            print("Starting rclone process...")
                            process = await asyncio.create_subprocess_exec(
                                rclone_path,
                                "--config", rclone_config,
                                "--progress",
                                "--stats", "1s",
                                "--stats-one-line",
                                "--transfers", "4",  # Reduced for memory efficiency
                                "--checkers", "8",    # Reduced for memory efficiency
                                "--buffer-size", "50M",
                                "--contimeout", "60s",
                                "--timeout", "300s",
                                "--retries", "3",
                                "--retries-sleep", "5s",
                                "copy",
                                temp_file_path,
                                f"b2:{B2_BUCKET_NAME}/{file_path}",
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE
                            )
                        except Exception as e:
                            print(f"Error starting rclone process: {str(e)}")
                            raise HTTPException(
                                status_code=500,
                                detail=f"Failed to start upload process: {str(e)}"
                            )
                            
                            # Monitor rclone progress in real-time
                            async def monitor_progress():
                                try:
                                    while True:
                                        line = await asyncio.wait_for(process.stdout.readline(), timeout=5.0)
                                        if not line:
                                            break
                                        line = line.decode().strip()
                                        if line:
                                            print(f"rclone progress: {line}")
                                        
                                        # Check for errors in stderr
                                        try:
                                            error_line = await asyncio.wait_for(process.stderr.readline(), timeout=0.1)
                                            if error_line:
                                                error_text = error_line.decode().strip()
                                                if error_text:
                                                    print(f"rclone error: {error_text}")
                                                    raise Exception(f"rclone error: {error_text}")
                                        except asyncio.TimeoutError:
                                            continue
                                except asyncio.TimeoutError:
                                    print("Progress monitoring timeout - process might be stuck")
                                    process.kill()
                                    raise Exception("Upload process timed out - no progress updates received")
                            
                            # Start progress monitoring
                            progress_task = asyncio.create_task(monitor_progress())
                            
                            # Wait for the process with timeout
                            try:
                                await asyncio.wait_for(process.wait(), timeout=600.0)  # 10 minute timeout
                                await progress_task
                                print("rclone process completed")
                            except asyncio.TimeoutError:
                                print("rclone process timed out")
                                process.kill()
                                raise HTTPException(
                                    status_code=408,
                                    detail="Upload timeout - process took too long"
                                )
                            except Exception as e:
                                print(f"Error during rclone process: {str(e)}")
                                process.kill()
                                raise HTTPException(
                                    status_code=500,
                                    detail=f"Failed to upload to B2: {str(e)}"
                                )
                            
                            if process.returncode != 0:
                                stderr_text = (await process.stderr.read()).decode()
                                print(f"rclone error: {stderr_text}")
                                raise HTTPException(
                                    status_code=500,
                                    detail=f"Failed to upload to B2: {stderr_text}"
                                )
                            
                            stdout_text = (await process.stdout.read()).decode()
                            print(f"rclone output: {stdout_text}")
                            
                            # Generate download URL
                            file_url = f"https://f003.backblazeb2.com/file/{B2_BUCKET_NAME}/{file_path}"
                            print(f"File uploaded successfully: {file_url}")
                            
                            return {
                                "url": file_url,
                                "filename": safe_filename,
                                "file_path": file_path,
                                "size": total_size,
                                "content_type": content_type
                            }
                            
                        except Exception as e:
                            print(f"rclone error: {str(e)}")
                            raise HTTPException(
                                status_code=500,
                                detail=f"Failed to upload to B2: {str(e)}"
                            )
                        finally:
                            # Clean up temporary file
                            try:
                                os.unlink(temp_file_path)
                                print("Temporary file cleaned up")
                            except Exception as e:
                                print(f"Error cleaning up temporary file: {str(e)}")
                except Exception as e:
                    print(f"Error processing file {file.filename}: {str(e)}")
                    raise HTTPException(
                        status_code=500,
                        detail=f"Error processing file {file.filename}: {str(e)}"
                    )

        # Process files in parallel
        for file in files:
            file_path = f"{unique_folder}/{file.filename}"
            content_type = file.content_type or mimetypes.guess_type(file.filename)[0] or 'application/octet-stream'
            upload_tasks.append(upload_single_file(file, file_path, content_type))

        # Wait for all uploads to complete
        files_data = await asyncio.gather(*upload_tasks)

        # Save metadata
        unique_id = str(uuid.uuid4())[:8]
        save_file_metadata(unique_id, files_data)
        print(f"Saved metadata for upload ID: {unique_id}")

        return JSONResponse(content={
            "message": "Upload successful",
            "files": files_data,
            "download_id": unique_id
        })

    except Exception as e:
        print(f"Unexpected error during upload: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")
    finally:
        # Clean up config file
        try:
            os.remove(rclone_config)
            print("Rclone config file cleaned up")
        except Exception as e:
            print(f"Error cleaning up rclone config: {str(e)}")

@app.get("/file/{file_id}")
async def file_page(file_id: str, background_tasks: BackgroundTasks):
    # Clean up expired files in the background
    cleanup_expired_files(background_tasks)
    
    file_data = get_file_metadata(file_id)
    
    if not file_data:
        return HTMLResponse(content="""
        <html><head><title>Fajlovi Nisu Pronađeni</title>
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
                <h1>Fajlovi Nisu Pronađeni</h1>
                <p>Izvinite, fajlovi koje tražite ne postoje ili su uklonjeni.</p>
                <a href="/">Povratak na Upload Stranicu</a>
            </div>
        </body>
        </html>
        """, status_code=404)

    files = file_data.get("files", [])
    upload_date = file_data.get("upload_date", 0)
    expiry_date = file_data.get("expiry_date", 0)
    
    # Format sizes
    def format_size(size_in_bytes):
        if size_in_bytes == 0:
            return '0 Bytes'
        k = 1024
        sizes = ['Bytes', 'KB', 'MB', 'GB']
        i = int(math.log(size_in_bytes) / math.log(k))
        return f"{size_in_bytes / math.pow(k, i):.2f} {sizes[i]}"
    
    # Format dates
    def format_date(timestamp):
        return datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')
    
    formatted_upload = format_date(upload_date) if upload_date else "Nepoznato"
    formatted_expiry = format_date(expiry_date) if expiry_date else "Nepoznato"
    
    days_left = max(0, int((expiry_date - time.time()) / (24 * 60 * 60))) if expiry_date else 0
    
    html_content = f"""<!DOCTYPE html>
    <html lang="sr">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Preuzmi Fajlove</title>
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
            
            .split-layout {{
                display: flex;
                width: 100%;
                height: 100vh;
                position: fixed;
                top: 0;
                left: 0;
                z-index: -1;
            }}
            
            .background-section {{
                flex: 1;
                position: relative;
                overflow: hidden;
                display: none;
            }}
            
            .background-image {{
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                object-fit: cover;
                opacity: 0;
                transition: opacity 0.5s ease;
            }}
            
            .background-image.active {{
                opacity: 1;
            }}
            
            .background-overlay {{
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: linear-gradient(45deg, rgba(0,0,0,0.3), rgba(0,0,0,0.1));
            }}
            
            .background-credit {{
                position: absolute;
                bottom: 20px;
                right: 20px;
                color: white;
                font-size: 12px;
                text-shadow: 0 1px 2px rgba(0,0,0,0.3);
                z-index: 2;
            }}
            
            .container {{
                background: white;
                border-radius: 12px;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
                width: 100%;
                max-width: 500px;
                padding: 32px;
                position: relative;
                z-index: 1;
            }}
            
            h1 {{
                text-align: center;
                font-weight: 600;
                font-size: 1.75rem;
                margin-bottom: 24px;
                color: #1d1d1f;
                line-height: 1.3;
            }}
            
            .files-list {{
                margin: 24px 0;
            }}
            
            .file-item {{
                padding: 16px;
                background-color: #f5f5f7;
                border-radius: 8px;
                margin-bottom: 12px;
                transition: all 0.2s ease;
            }}
            
            .file-item:hover {{
                background-color: #e5e5e7;
            }}
            
            .file-name {{
                font-weight: 500;
                color: #1d1d1f;
                font-size: 1rem;
                margin-bottom: 8px;
                word-break: break-all;
            }}
            
            .file-meta {{
                display: flex;
                justify-content: space-between;
                font-size: 14px;
                color: #86868b;
                margin-bottom: 12px;
            }}
            
            .download-button {{
                display: block;
                width: 100%;
                background-color: #0071e3;
                color: white;
                text-align: center;
                text-decoration: none;
                padding: 8px;
                border-radius: 6px;
                font-weight: 500;
                transition: all 0.2s ease;
                font-size: 14px;
            }}
            
            .download-button:hover {{
                background-color: #0077ed;
            }}
            
            .download-all-button {{
                display: block;
                width: 100%;
                background-color: #34c759;
                color: white;
                text-align: center;
                text-decoration: none;
                padding: 12px;
                border-radius: 8px;
                font-weight: 500;
                transition: all 0.2s ease;
                margin-bottom: 16px;
                font-size: 16px;
            }}
            
            .download-all-button:hover {{
                background-color: #30b751;
                transform: translateY(-1px);
            }}
            
            .upload-info {{
                margin-top: 24px;
                padding: 16px;
                background-color: #f5f5f7;
                border-radius: 8px;
                font-size: 14px;
                color: #86868b;
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
            
            .download-progress {{
                display: none;
                margin-top: 16px;
                padding: 16px;
                background-color: #f5f5f7;
                border-radius: 8px;
            }}
            
            .progress-bar {{
                height: 6px;
                width: 100%;
                background-color: #e5e5e5;
                border-radius: 3px;
                overflow: hidden;
                margin-bottom: 8px;
            }}
            
            .progress-fill {{
                height: 100%;
                width: 0%;
                background-color: #34c759;
                transition: width 0.3s ease;
            }}
            
            .progress-text {{
                font-size: 14px;
                color: #86868b;
                text-align: center;
            }}
            
            .copyright {{
                position: fixed;
                bottom: 20px;
                left: 50%;
                transform: translateX(-50%);
                color: #86868b;
                font-size: 12px;
                text-align: center;
                width: 100%;
                padding: 0 20px;
                z-index: 2;
            }}
            
            @media (min-width: 1024px) {{
                .background-section {{
                    display: block;
                }}
            }}
            
            @media (max-width: 768px) {{
                .container {{
                    padding: 24px 16px;
                }}
                
                h1 {{
                    font-size: 1.5rem;
                }}
                
                .file-item {{
                    padding: 12px;
                }}
            }}
        </style>
        <script>
            async function downloadAllFiles() {{
                const downloadAllButton = document.getElementById('downloadAllButton');
                const downloadProgress = document.getElementById('downloadProgress');
                const progressFill = document.getElementById('progressFill');
                const progressText = document.getElementById('progressText');
                
                downloadAllButton.style.display = 'none';
                downloadProgress.style.display = 'block';
                
                const files = {files};
                let downloadedCount = 0;
                
                for (const file of files) {{
                    progressText.textContent = `Preuzimanje ${{file.filename}}...`;
                    progressFill.style.width = '0%';
                    
                    try {{
                        const response = await fetch(`/download/{file_id}/${{file.filename}}`);
                        if (!response.ok) throw new Error('Preuzimanje nije uspelo');
                        
                        const blob = await response.blob();
                        const url = window.URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = file.filename;
                        document.body.appendChild(a);
                        a.click();
                        window.URL.revokeObjectURL(url);
                        document.body.removeChild(a);
                        
                        downloadedCount++;
                        progressFill.style.width = `${{(downloadedCount / files.length) * 100}}%`;
                        progressText.textContent = `Preuzeto ${{downloadedCount}} od ${{files.length}} fajlova`;
                    }} catch (error) {{
                        console.error('Greška pri preuzimanju fajla:', error);
                        progressText.textContent = `Greška pri preuzimanju ${{file.filename}}. Nastavljam sa sledećim...`;
                    }}
                }}
                
                progressText.textContent = 'Svi fajlovi preuzeti!';
                setTimeout(() => {{
                    downloadAllButton.style.display = 'block';
                    downloadProgress.style.display = 'none';
                }}, 2000);
            }}
            
            // Background images rotation
            const backgroundImages = [
                {{
                    url: "https://images.pexels.com/photos/2387873/pexels-photo-2387873.jpeg",
                    credit: "Foto: Francesco Ungaro na Pexels"
                }},
                {{
                    url: "https://images.pexels.com/photos/2387876/pexels-photo-2387876.jpeg",
                    credit: "Foto: Francesco Ungaro na Pexels"
                }},
                {{
                    url: "https://images.pexels.com/photos/2387877/pexels-photo-2387877.jpeg",
                    credit: "Foto: Francesco Ungaro na Pexels"
                }}
            ];
            
            let currentImageIndex = 0;
            const backgroundSection = document.querySelector('.background-section');
            const backgroundCredit = document.querySelector('.background-credit');
            
            function createBackgroundImage(url) {{
                const img = document.createElement('img');
                img.src = url;
                img.className = 'background-image';
                img.onload = function() {{
                    img.classList.add('active');
                }};
                img.onerror = function() {{
                    console.error('Failed to load background image:', url);
                    // Try to load a fallback image
                    img.src = 'https://images.pexels.com/photos/2387873/pexels-photo-2387873.jpeg';
                }};
                return img;
            }}
            
            function rotateBackground() {{
                if (!backgroundSection) return;
                
                const currentImage = document.querySelector('.background-image.active');
                const nextImage = createBackgroundImage(backgroundImages[currentImageIndex].url);
                
                backgroundSection.appendChild(nextImage);
                
                if (currentImage) {{
                    currentImage.classList.remove('active');
                    setTimeout(() => currentImage.remove(), 500);
                }}
                
                if (backgroundCredit) {{
                    backgroundCredit.textContent = backgroundImages[currentImageIndex].credit;
                }}
                
                currentImageIndex = (currentImageIndex + 1) % backgroundImages.length;
            }}
            
            // Initialize background images when the page loads
            document.addEventListener('DOMContentLoaded', function() {{
                if (backgroundSection) {{
                    // Create and add the first image
                    const firstImage = createBackgroundImage(backgroundImages[0].url);
                    backgroundSection.appendChild(firstImage);
                    if (backgroundCredit) {{
                        backgroundCredit.textContent = backgroundImages[0].credit;
                    }}
                    
                    // Start rotation after a delay
                    setTimeout(() => {{
                        rotateBackground();
                        setInterval(rotateBackground, 10000);
                    }}, 5000);
                }}
            }});
        </script>
    </head>
    <body>
        <div class="split-layout">
            <div class="background-section">
                <div class="background-overlay"></div>
                <div class="background-credit"></div>
            </div>
        </div>
        
        <div class="container">
            <h1>Vaši Fajlovi Su Spremni</h1>
            
            <a href="#" onclick="downloadAllFiles(); return false;" id="downloadAllButton" class="download-all-button">
                Preuzmi Sve Fajlove
            </a>
            
            <div id="downloadProgress" class="download-progress">
                <div class="progress-bar">
                    <div id="progressFill" class="progress-fill"></div>
                </div>
                <div id="progressText" class="progress-text">Pripremam preuzimanje...</div>
            </div>
            
            <div class="files-list">
                {''.join(f'''
                <div class="file-item">
                    <div class="file-name">{file["filename"]}</div>
                    <div class="file-meta">
                        <span>Veličina: {format_size(file["size"])}</span>
                    </div>
                    <a href="/download/{file_id}/{file["filename"]}" class="download-button">Preuzmi {file["filename"]}</a>
                </div>
                ''' for file in files)}
            </div>
            
            <div class="upload-info">
                <div>Uploadovano: {formatted_upload}</div>
                <div>Ističe: {formatted_expiry}</div>
            </div>
            
            <p class="expire-notice">Ovi fajlovi će istući za {days_left+1} dan{"a" if days_left != 1 else ""}</p>
            
            <a href="/" class="back-link">Uploaduj još fajlova</a>
        </div>
        
        <div class="copyright">© """ + str(datetime.datetime.now().year) + """ Luka Trbović. Sva prava zadržana.</div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/download/{file_id}/{filename}")
async def download_file(file_id: str, filename: str, background_tasks: BackgroundTasks):
    # Clean up expired files in the background
    cleanup_expired_files(background_tasks)
    
    try:
        file_data = get_file_metadata(file_id)
        if not file_data:
            raise HTTPException(status_code=404, detail="Files not found")
        
        # Check if files have expired
        if file_data.get("expiry_date", 0) < int(time.time()):
            raise HTTPException(status_code=410, detail="Files have expired")
        
        # Find the requested file in the files list
        requested_file = next((f for f in file_data.get("files", []) if f["filename"] == filename), None)
        if not requested_file:
            raise HTTPException(status_code=404, detail="File not found")
        
        file_url = requested_file["url"]
        content_type = requested_file.get("content_type", "application/octet-stream")
        
        # Set up httpx client with proper timeout and limits
        async def file_stream():
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
                async with client.stream('GET', file_url) as response:
                    if response.status_code != 200:
                        raise HTTPException(
                            status_code=response.status_code,
                            detail="Failed to fetch file from remote server"
                        )
                    
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):  # 64KB chunks
                        yield chunk
        
        # Set appropriate headers for the response
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
        
        return StreamingResponse(
            file_stream(),
            media_type=content_type,
            headers=headers
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error downloading file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=80)