from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import os, json, uuid, time, datetime, shutil, math, re, tempfile, subprocess, asyncio, platform, zipfile
from pathlib import Path
from typing import Dict, Any
from b2sdk.v2 import B2Api, InMemoryAccountInfo
import httpx
import mimetypes
import psutil

# B2 Configuration
B2_APPLICATION_KEY_ID = "bec925575d01"
B2_APPLICATION_KEY = "0036d7b3f5dfb4423881abfaaca8d4162e3ae570e1"
B2_BUCKET_NAME = "fdmbucket"

# File configuration
FILES_DB = "files.json"
FILE_EXPIRY_DAYS = 7

# Storage configuration - optimized for 100GB disk and 2GB RAM
STORAGE_BASE_DIR = "/mnt/disk"  # Base directory for all storage operations
TEMP_UPLOAD_DIR = os.path.join(STORAGE_BASE_DIR, "temp_uploads")  # Temporary upload directory
TOOLS_DIR = os.path.join(STORAGE_BASE_DIR, "tools")  # Tools directory

# Calculate available memory and storage
TOTAL_MEMORY = psutil.virtual_memory().total
TOTAL_STORAGE = 100 * 1024 * 1024 * 1024  # 100GB dedicated storage

# Dynamic resource allocation based on system specs
MAX_TEMP_STORAGE = min(85 * 1024 * 1024 * 1024, TOTAL_STORAGE * 0.85)  # 85GB or 85% of storage for temp files
CHUNK_SIZE = min(32 * 1024 * 1024, TOTAL_MEMORY // 8)  # 32MB chunks or 1/8 of RAM
MAX_CONCURRENT_UPLOADS = 4  # Limited for single vCPU
MEMORY_BUFFER = min(256 * 1024 * 1024, TOTAL_MEMORY // 4)  # 256MB or 1/4 of RAM
CACHE_EXPIRY = 3 * 60 * 60  # 3 hours cache expiry (reduced to help manage storage better)

# Background images configuration
BACKGROUND_IMAGES = [
    {
        "url": "https://f004.backblazeb2.com/file/fdmbucket/backgrounds/bg1.jpg",
        "credit": "Foto: Francesco Ungaro na Pexels"
    },
    {
        "url": "https://f004.backblazeb2.com/file/fdmbucket/backgrounds/bg2.jpg",
        "credit": "Foto: Francesco Ungaro na Pexels"
    },
    {
        "url": "https://f004.backblazeb2.com/file/fdmbucket/backgrounds/bg3.jpg",
        "credit": "Foto: Francesco Ungaro na Pexels"
    }
]

# Ensure directories exist with proper permissions
for directory in [STORAGE_BASE_DIR, TEMP_UPLOAD_DIR, TOOLS_DIR]:
    try:
        os.makedirs(directory, exist_ok=True)
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
    try:
        for dirpath, dirnames, filenames in os.walk(TEMP_UPLOAD_DIR):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.exists(fp):  # Check if file still exists
                    try:
                        total_size += os.path.getsize(fp)
                    except OSError:
                        continue
    except Exception as e:
        print(f"Error calculating temp storage usage: {str(e)}")
    return total_size

def get_storage_stats():
    """Get current storage statistics"""
    try:
        usage = psutil.disk_usage(STORAGE_BASE_DIR)
        temp_usage = get_temp_storage_usage()
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "temp_usage": temp_usage,
            "percent": usage.percent
        }
    except Exception as e:
        print(f"Error getting storage stats: {str(e)}")
        return None

def should_accept_upload(file_size: int) -> bool:
    """Check if we can accept a new upload based on storage availability"""
    try:
        stats = get_storage_stats()
        if not stats:
            return False
            
        # Calculate available temp storage
        available_temp = MAX_TEMP_STORAGE - stats["temp_usage"]
        
        # We need at least file_size + 500MB buffer available (reduced buffer for larger files)
        buffer_size = 500 * 1024 * 1024  # 500MB buffer
        
        # For very large files (>30GB), reduce buffer requirement
        if file_size > 30 * 1024 * 1024 * 1024:
            buffer_size = 250 * 1024 * 1024  # 250MB buffer for large files
        
        # Check both temp storage and overall storage
        return (available_temp >= file_size + buffer_size and 
                stats["free"] >= file_size + buffer_size)
    except Exception as e:
        print(f"Error checking storage availability: {str(e)}")
        return False

def cleanup_temp_storage():
    """Clean up temporary storage if it exceeds limits"""
    try:
        stats = get_storage_stats()
        if not stats:
            return
            
        current_usage = stats["temp_usage"]
        storage_percent = stats["percent"]
        
        # Clean up if temp storage exceeds limit OR overall storage is >95% full
        if current_usage > MAX_TEMP_STORAGE or storage_percent > 95:
            print(f"Storage cleanup needed: Temp usage: {current_usage / (1024**3):.2f}GB, Storage used: {storage_percent}%")
            current_time = time.time()
            
            # If storage is very tight (>98%), be more aggressive with cleanup
            aggressive_cleanup = storage_percent > 98
            cleanup_threshold = CACHE_EXPIRY // 3 if aggressive_cleanup else CACHE_EXPIRY
            
            # Sort files by age and size
            files_to_clean = []
            for dirpath, dirnames, filenames in os.walk(TEMP_UPLOAD_DIR):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        if os.path.exists(fp):
                            stat = os.stat(fp)
                            files_to_clean.append({
                                'path': fp,
                                'size': stat.st_size,
                                'age': current_time - stat.st_mtime
                            })
                    except Exception:
                        continue
            
            # Sort by age (oldest first) and size (largest first)
            files_to_clean.sort(key=lambda x: (-x['age'], -x['size']))
            
            # Delete files until we're under the threshold
            for file_info in files_to_clean:
                try:
                    if (current_usage > MAX_TEMP_STORAGE * 0.9 or  # Keep deleting until we're at 90%
                        storage_percent > 95 or 
                        file_info['age'] > cleanup_threshold):
                        os.remove(file_info['path'])
                        print(f"Deleted temporary file: {file_info['path']} (Size: {file_info['size'] / (1024**2):.2f}MB, Age: {file_info['age'] / 3600:.1f}h)")
                        current_usage -= file_info['size']
                        if current_usage <= MAX_TEMP_STORAGE * 0.8 and storage_percent <= 90:
                            break
                except Exception as e:
                    print(f"Error deleting temporary file {file_info['path']}: {str(e)}")
                        
            # After cleanup, check if we need to alert about storage issues
            new_stats = get_storage_stats()
            if new_stats and new_stats["percent"] > 98:
                print("WARNING: Storage usage remains critical after cleanup!")
    except Exception as e:
        print(f"Error during temp storage cleanup: {str(e)}")

# Function to ensure rclone is available with optimized configuration
def ensure_rclone():
    """Ensure rclone is available with optimized configuration"""
    rclone_dir = TOOLS_DIR
    system = platform.system().lower()
    rclone_exe = os.path.join(rclone_dir, "rclone.exe" if system == "windows" else "rclone")
    
    if os.path.exists(rclone_exe):
        return rclone_exe
        
    print("Rclone not found. Downloading...")
    
    try:
        # Download and setup rclone
        download_url = {
            "windows": "https://downloads.rclone.org/rclone-current-windows-amd64.zip",
            "darwin": "https://downloads.rclone.org/rclone-current-osx-amd64.zip"
        }.get(system, "https://downloads.rclone.org/rclone-current-linux-amd64.zip")
        
        zip_path = os.path.join(rclone_dir, "rclone.zip")
        
        with httpx.Client() as client:
            response = client.get(download_url)
            response.raise_for_status()
            
            with open(zip_path, 'wb') as f:
                f.write(response.content)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(rclone_dir)
        
        # Find extracted directory
        extracted_dir = next(
            (d for d in os.listdir(rclone_dir) 
             if d.startswith("rclone-") and os.path.isdir(os.path.join(rclone_dir, d))),
            None
        )
        
        if not extracted_dir:
            raise Exception("Could not find extracted rclone directory")
        
        # Move executable
        src_exe = os.path.join(rclone_dir, extracted_dir, 
                              "rclone.exe" if system == "windows" else "rclone")
        shutil.copy2(src_exe, rclone_exe)
        
        # Set permissions on Unix-like systems
        if system != "windows":
            os.chmod(rclone_exe, 0o755)
        
        # Clean up
        os.remove(zip_path)
        shutil.rmtree(os.path.join(rclone_dir, extracted_dir))
        
        print(f"Rclone downloaded and installed at {rclone_exe}")
        return rclone_exe
        
    except Exception as e:
        print(f"Error downloading rclone: {str(e)}")
        raise Exception(f"Failed to download rclone: {str(e)}")

# Create optimized rclone configuration
def create_rclone_config():
    """Create optimized rclone configuration file"""
    config_path = os.path.join(os.getcwd(), "rclone.conf")
    try:
        with open(config_path, "w") as f:
            f.write(f"""[b2]
type = b2
account = {B2_APPLICATION_KEY_ID}
key = {B2_APPLICATION_KEY}
hard_delete = true
upload_cutoff = {CHUNK_SIZE}
chunk_size = {CHUNK_SIZE}
max_upload_parts = 10000
max_upload_concurrency = {MAX_CONCURRENT_UPLOADS}
memory_pool_flush_time = 1m
memory_pool_use_mmap = false
use_mmap = false
""")
        return config_path
    except Exception as e:
        print(f"Error creating rclone config: {str(e)}")
        raise

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
    
    # Convert background images to JavaScript array
    background_images_js = json.dumps(BACKGROUND_IMAGES)
    
    return f"""
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
                document.addEventListener("DOMContentLoaded", function() {{
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
                    const backgroundSection = document.querySelector('.background-section');
                    const backgroundCredit = document.querySelector('.background-credit');
                    
                    // Initialize state variables
                    let selectedFiles = [];
                    let uploadStartTime = null;
                    let lastUploadedBytes = 0;
                    let lastSpeedUpdateTime = null;
                    
                    // Initialize background images
                    const backgroundImages = {background_images_js};
                    let currentImageIndex = 0;
                    
                    function createBackgroundImage(imageData) {{
                        const img = document.createElement('img');
                        img.src = imageData.url;
                        img.className = 'background-image';
                        img.onload = function() {{
                            img.classList.add('active');
                        }};
                        return img;
                    }}
                    
                    function rotateBackground() {{
                        if (!backgroundSection) return;
                        
                        const currentImage = document.querySelector('.background-image.active');
                        const nextImageData = backgroundImages[currentImageIndex];
                        const nextImage = createBackgroundImage(nextImageData);
                        
                        backgroundSection.appendChild(nextImage);
                        
                        if (currentImage) {{
                            currentImage.classList.remove('active');
                            setTimeout(() => currentImage.remove(), 500);
                        }}
                        
                        if (backgroundCredit) {{
                            backgroundCredit.textContent = nextImageData.credit;
                        }}
                        
                        currentImageIndex = (currentImageIndex + 1) % backgroundImages.length;
                    }}
                    
                    // Initialize background
                    if (backgroundSection && backgroundImages.length > 0) {{
                        const firstImage = createBackgroundImage(backgroundImages[0]);
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
                }});
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
        # Calculate total upload size
        total_size = sum(f.size for f in files)
        
        # Check if we can accept the upload
        if not should_accept_upload(total_size):
            raise HTTPException(
                status_code=507,
                detail="Insufficient storage space available. Please try again later."
            )
        
        # Ensure rclone is available
        rclone_path = ensure_rclone()
        unique_folder = generate_unique_folder()
        print(f"\n=== Starting new upload session ===")
        print(f"Number of files: {len(files)}")
        print(f"Total size: {total_size / (1024**3):.2f}GB")
        print(f"Generated unique folder: {unique_folder}")

        # Create optimized rclone config
        rclone_config = create_rclone_config()

        files_data = []
        upload_tasks = []
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)

        async def upload_single_file(file: UploadFile, file_path: str, content_type: str) -> dict:
            async with semaphore:
                try:
                    if not file.filename:
                        raise HTTPException(status_code=400, detail="File name is required")
                    
                    print(f"\n=== Processing file: {file.filename} ===")
                    print(f"File size: {file.size / (1024**3):.2f}GB")
                    
                    # Validate filename
                    safe_filename = file.filename.encode('utf-8').decode('utf-8')
                    safe_filename = "".join(c for c in safe_filename if c.isprintable())
                    validate_b2_filename(safe_filename)
                    
                    # Create a unique temporary file path
                    temp_file_path = os.path.join(TEMP_UPLOAD_DIR, f"{uuid.uuid4()}_{safe_filename}")
                    
                    try:
                        print("Starting file read...")
                        total_size = 0
                        last_progress_time = time.time()
                        last_progress_size = 0
                        
                        # Open file in binary write mode with optimized buffering
                        with open(temp_file_path, 'wb', buffering=MEMORY_BUFFER) as temp_file:
                            while True:
                                try:
                                    chunk = await asyncio.wait_for(file.read(CHUNK_SIZE), timeout=30.0)
                                    if not chunk:
                                        break
                                    temp_file.write(chunk)
                                    total_size += len(chunk)
                                    
                                    current_time = time.time()
                                    if current_time - last_progress_time >= 2:
                                        bytes_since_last = total_size - last_progress_size
                                        speed = bytes_since_last / (current_time - last_progress_time)
                                        print(f"Progress: {total_size / (1024**3):.2f}GB written ({format(total_size/file.size*100, '.1f')}%)")
                                        print(f"Current speed: {format(speed/1024/1024, '.1f')} MB/s")
                                        last_progress_time = current_time
                                        last_progress_size = total_size
                                        
                                        # Check storage status during upload
                                        stats = get_storage_stats()
                                        if stats and stats["percent"] > 95:
                                            print("WARNING: Storage usage critical during upload!")
                                        
                                except asyncio.TimeoutError:
                                    print("Upload timeout - connection too slow")
                                    raise HTTPException(
                                        status_code=408,
                                        detail="Upload timeout - connection too slow"
                                    )
                        
                        print("File read complete, starting B2 upload...")
                        
                        # Use rclone for upload with optimized settings
                        b2_path = f"b2:{B2_BUCKET_NAME}/{file_path}"
                        
                        # Run rclone copy command with progress monitoring
                        process = await asyncio.create_subprocess_exec(
                            rclone_path,
                            "--config", rclone_config,
                            "copy",
                            "--no-traverse",
                            "--transfers", str(MAX_CONCURRENT_UPLOADS),
                            "--checkers", "4",
                            "--contimeout", "30s",
                            "--timeout", "30s",
                            "--retries", "3",
                            "--low-level-retries", "10",
                            "--stats", "1s",
                            "--stats-one-line",
                            "--stats-log-level", "NOTICE",
                            temp_file_path,
                            b2_path,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE
                        )
                        
                        # Monitor upload progress
                        while True:
                            try:
                                stdout_data = await asyncio.wait_for(process.stdout.readline(), timeout=1.0)
                                if not stdout_data:
                                    break
                                print(f"Rclone progress: {stdout_data.decode().strip()}")
                            except asyncio.TimeoutError:
                                continue
                            
                        await process.wait()
                        if process.returncode != 0:
                            stderr = await process.stderr.read()
                            print(f"Rclone error: {stderr.decode()}")
                            raise Exception(f"Failed to upload file: {stderr.decode()}")
                            
                        # Generate download URL using B2 bucket URL
                        file_url = f"https://f004.backblazeb2.com/file/{B2_BUCKET_NAME}/{file_path}"
                        print(f"File uploaded successfully: {file_url}")
                        
                        return {
                            "url": file_url,
                            "filename": safe_filename,
                            "file_path": file_path,
                            "size": total_size,
                            "content_type": content_type
                        }
                        
                    except Exception as e:
                        print(f"Error processing file {file.filename}: {str(e)}")
                        raise HTTPException(
                            status_code=500,
                            detail=f"Error processing file {file.filename}: {str(e)}"
                        )
                    finally:
                        # Clean up temporary file
                        try:
                            if os.path.exists(temp_file_path):
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

        # Process files in parallel with resource limits
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

        # Final storage check
        stats = get_storage_stats()
        if stats and stats["percent"] > 90:
            print(f"WARNING: High storage usage after upload: {stats['percent']}%")
            # Trigger cleanup in background
            cleanup_temp_storage()

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
            if os.path.exists(rclone_config):
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
    
    # Convert background images to JavaScript array
    background_images_js = json.dumps(BACKGROUND_IMAGES)
    
    return f"""<!DOCTYPE html>
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
            
            // Background images configuration
            const backgroundImages = {background_images_js};
            let currentImageIndex = 0;
            
            document.addEventListener('DOMContentLoaded', function() {{
                const backgroundSection = document.querySelector('.background-section');
                const backgroundCredit = document.querySelector('.background-credit');
                
                function createBackgroundImage(imageData) {{
                    const img = document.createElement('img');
                    img.src = imageData.url;
                    img.className = 'background-image';
                    img.onload = function() {{
                        img.classList.add('active');
                    }};
                    return img;
                }}
                
                function rotateBackground() {{
                    if (!backgroundSection) return;
                    
                    const currentImage = document.querySelector('.background-image.active');
                    const nextImageData = backgroundImages[currentImageIndex];
                    const nextImage = createBackgroundImage(nextImageData);
                    
                    backgroundSection.appendChild(nextImage);
                    
                    if (currentImage) {{
                        currentImage.classList.remove('active');
                        setTimeout(() => currentImage.remove(), 500);
                    }}
                    
                    if (backgroundCredit) {{
                        backgroundCredit.textContent = nextImageData.credit;
                    }}
                    
                    currentImageIndex = (currentImageIndex + 1) % backgroundImages.length;
                }}
                
                // Initialize background
                if (backgroundSection && backgroundImages.length > 0) {{
                    const firstImage = createBackgroundImage(backgroundImages[0]);
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
        
        # Use rclone to stream the file
        rclone_path = ensure_rclone()
        rclone_config = create_rclone_config()
        
        async def file_stream():
            try:
                process = await asyncio.create_subprocess_exec(
                    rclone_path,
                    "--config", rclone_config,
                    "cat",
                    "--no-traverse",
                    "--contimeout", "30s",
                    "--timeout", "30s",
                    "--retries", "3",
                    "--low-level-retries", "10",
                    f"b2:{B2_BUCKET_NAME}/{requested_file['file_path']}",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                
                while True:
                    chunk = await process.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
                    
                await process.wait()
                if process.returncode != 0:
                    stderr = await process.stderr.read()
                    print(f"Rclone error: {stderr.decode()}")
                    raise Exception(f"Failed to download file: {stderr.decode()}")
            finally:
                # Clean up config
                try:
                    if os.path.exists(rclone_config):
                        os.remove(rclone_config)
                except Exception as e:
                    print(f"Error cleaning up rclone config: {str(e)}")
        
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