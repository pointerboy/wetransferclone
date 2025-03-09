from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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
B2_ENDPOINT = f"https://f003.backblazeb2.com/file/{B2_BUCKET_NAME}"  # Direct endpoint for downloads

# File configuration
FILES_DB = "files.json"
FILE_EXPIRY_DAYS = 14

# Storage configuration - optimized for 100GB disk and 2GB RAM
STORAGE_BASE_DIR = "/mnt/disk"  # Base directory for all storage operations
TEMP_UPLOAD_DIR = os.path.join(STORAGE_BASE_DIR, "temp_uploads")  # Temporary upload directory
TOOLS_DIR = os.path.join(STORAGE_BASE_DIR, "tools")  # Tools directory

# Calculate available memory and storage
TOTAL_MEMORY = psutil.virtual_memory().total
TOTAL_STORAGE = 100 * 1024 * 1024 * 1024  # 100GB dedicated storage

# Dynamic resource allocation based on system specs
MAX_TEMP_STORAGE = min(90 * 1024 * 1024 * 1024, TOTAL_STORAGE * 0.9)  # 90GB or 90% of storage for temp files
CHUNK_SIZE = min(32 * 1024 * 1024, TOTAL_MEMORY // 8)  # 32MB chunks or 1/8 of RAM
MAX_CONCURRENT_UPLOADS = 4  # Limited for single vCPU
MEMORY_BUFFER = min(256 * 1024 * 1024, TOTAL_MEMORY // 4)  # 256MB or 1/4 of RAM
CACHE_EXPIRY = 3 * 60 * 60  # 3 hours cache expiry

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

def ensure_rclone():
    """Ensure rclone is available with optimized configuration"""
    # First try to find rclone in system PATH
    try:
        result = subprocess.run(['which' if platform.system() != 'Windows' else 'where', 'rclone'], 
                              capture_output=True, text=True)
        if result.returncode == 0:
            rclone_path = result.stdout.strip()
            print(f"Found system rclone at: {rclone_path}")
            return rclone_path
    except Exception as e:
        print(f"System rclone not found: {str(e)}")

    # If not in PATH, check our tools directory
    rclone_dir = TOOLS_DIR
    system = platform.system().lower()
    rclone_exe = os.path.join(rclone_dir, "rclone.exe" if system == "windows" else "rclone")
    
    if os.path.exists(rclone_exe):
        print(f"Found local rclone at: {rclone_exe}")
        return rclone_exe

    print("Downloading rclone...")
    
    try:
        # Determine correct download URL based on system architecture
        arch = platform.machine().lower()
        arch = 'amd64' if arch in ['x86_64', 'amd64'] else 'arm64' if arch in ['arm64', 'aarch64'] else arch
        
        base_url = "https://downloads.rclone.org/rclone-current-"
        download_url = {
            "windows": f"{base_url}windows-{arch}.zip",
            "darwin": f"{base_url}osx-{arch}.zip",
            "linux": f"{base_url}linux-{arch}.zip"
        }.get(system)

        if not download_url:
            raise Exception(f"Unsupported system: {system} {arch}")

        print(f"Downloading rclone from: {download_url}")
        
        # Create temporary directory for download
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "rclone.zip")
            
            # Download with progress tracking
            with httpx.Client() as client:
                with client.stream('GET', download_url) as response:
                    response.raise_for_status()
                    total = int(response.headers.get('content-length', 0))
                    
                    with open(zip_path, 'wb') as f:
                        for chunk in response.iter_bytes():
                            f.write(chunk)
            
            # Extract and setup
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            
            # Find extracted directory
            extracted_dir = next(
                (d for d in os.listdir(temp_dir) 
                 if d.startswith("rclone-") and os.path.isdir(os.path.join(temp_dir, d))),
                None
            )
            
            if not extracted_dir:
                raise Exception("Could not find extracted rclone directory")
            
            # Ensure tools directory exists
            os.makedirs(rclone_dir, exist_ok=True)
            
            # Move executable to final location
            src_exe = os.path.join(temp_dir, extracted_dir, 
                                 "rclone.exe" if system == "windows" else "rclone")
            
            shutil.copy2(src_exe, rclone_exe)
            
            # Set executable permissions on Unix-like systems
            if system != "windows":
                os.chmod(rclone_exe, 0o755)
            
            print(f"Rclone installed successfully at: {rclone_exe}")
            
            # Verify installation
            try:
                result = subprocess.run([rclone_exe, "--version"], 
                                     capture_output=True, text=True)
                if result.returncode == 0:
                    print(f"Rclone version: {result.stdout.split('\n')[0]}")
                else:
                    raise Exception(f"Rclone verification failed: {result.stderr}")
            except Exception as e:
                print(f"Warning: Could not verify rclone installation: {str(e)}")
            
            return rclone_exe
            
    except Exception as e:
        error_msg = f"Failed to download/setup rclone: {str(e)}"
        print(error_msg)
        raise Exception(error_msg)

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

# Initialize templates
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def upload_page(request: Request, background_tasks: BackgroundTasks):
    # Clean up expired files in the background
    cleanup_expired_files(background_tasks)
    
    return templates.TemplateResponse("upload.html", {
        "request": request,
        "background_images": BACKGROUND_IMAGES,
        "expiry_days": FILE_EXPIRY_DAYS,
        "year": datetime.datetime.now().year
    })

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
                                        
                                except asyncio.TimeoutError:
                                    print("Upload timeout - connection too slow")
                                    raise HTTPException(
                                        status_code=408,
                                        detail="Upload timeout - connection too slow"
                                    )
                        
                        print("File read complete, starting B2 upload...")
                        
                        # Upload to B2 using rclone
                        upload_success = await upload_to_b2(temp_file_path, file_path, rclone_path, rclone_config)
                        
                        if not upload_success:
                            raise Exception("Failed to upload file to B2")
                            
                        # Generate download URL
                        file_url = f"{B2_ENDPOINT}/{file_path}"
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

@app.get("/file/{file_id}", response_class=HTMLResponse)
async def file_page(request: Request, file_id: str, background_tasks: BackgroundTasks):
    # Clean up expired files in the background
    cleanup_expired_files(background_tasks)
    
    file_data = get_file_metadata(file_id)
    
    if not file_data:
        return templates.TemplateResponse("error.html", {
            "request": request,
            "error_title": "Fajlovi Nisu Pronađeni",
            "error_message": "Izvinite, fajlovi koje tražite ne postoje ili su uklonjeni.",
            "background_images": BACKGROUND_IMAGES,
            "year": datetime.datetime.now().year
        }, status_code=404)

    files = file_data.get("files", [])
    upload_date = file_data.get("upload_date", 0)
    expiry_date = file_data.get("expiry_date", 0)
    
    # Format sizes and prepare file data
    formatted_files = []
    for file in files:
        size = file.get("size", 0)
        formatted_size = format_size(size)
        formatted_files.append({
            "filename": file["filename"],
            "size": size,
            "size_formatted": formatted_size,
            "download_url": f"/download/{file_id}/{file['filename']}"
        })
    
    # Format dates
    formatted_upload = format_date(upload_date) if upload_date else "Nepoznato"
    formatted_expiry = format_date(expiry_date) if expiry_date else "Nepoznato"
    
    days_left = max(0, int((expiry_date - time.time()) / (24 * 60 * 60))) if expiry_date else 0
    
    return templates.TemplateResponse("download.html", {
        "request": request,
        "files": formatted_files,
        "upload_date": formatted_upload,
        "expiry_date": formatted_expiry,
        "days_left": days_left,
        "background_images": BACKGROUND_IMAGES,
        "year": datetime.datetime.now().year
    })

def format_size(size_in_bytes):
    if size_in_bytes == 0:
        return '0 Bytes'
    k = 1024
    sizes = ['Bytes', 'KB', 'MB', 'GB']
    i = int(math.log(size_in_bytes) / math.log(k))
    return f"{size_in_bytes / math.pow(k, i):.2f} {sizes[i]}"

def format_date(timestamp):
    return datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')

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