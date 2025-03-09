// Utility functions
function formatBytes(bytes, decimals = 2) {
    if (bytes == 0) return '0 Bytes';
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

// File handling
class FileUploader {
    constructor() {
        this.selectedFiles = [];
        this.uploadStartTime = null;
        this.lastUploadedBytes = 0;
        this.lastSpeedUpdateTime = null;
        this.totalProgress = 0;
        this.uploadPhase = 'preparing'; // preparing, uploading, transferring, complete
        
        this.initializeElements();
        this.setupEventListeners();
    }
    
    initializeElements() {
        this.uploadArea = document.getElementById("uploadArea");
        this.fileInput = document.getElementById("fileInput");
        this.filesList = document.getElementById("filesList");
        this.uploadButton = document.getElementById("uploadButton");
        this.loaderContainer = document.getElementById("loaderContainer");
        this.progressFill = document.getElementById("progressFill");
        this.percentText = document.getElementById("percentText");
        this.uploadSpeed = document.getElementById("uploadSpeed");
        this.statusText = document.getElementById("status");
        this.linkContainer = document.getElementById("linkContainer");
        this.copyButton = document.getElementById("copyButton");
        this.linkUrl = document.getElementById("linkUrl");
        
        // Create notification element
        this.notification = document.createElement('div');
        this.notification.className = 'copied-notification';
        this.notification.textContent = 'Link kopiran!';
        document.body.appendChild(this.notification);
    }
    
    setupEventListeners() {
        if (this.uploadArea) {
            this.uploadArea.addEventListener('click', () => {
                if (this.fileInput) {
                    this.fileInput.click();
                }
            });

            ['dragover', 'dragenter'].forEach(eventName => {
                this.uploadArea.addEventListener(eventName, (e) => {
                    e.preventDefault();
                    this.uploadArea.classList.add('dragover');
                });
            });

            ['dragleave', 'dragend'].forEach(eventName => {
                this.uploadArea.addEventListener(eventName, () => {
                    this.uploadArea.classList.remove('dragover');
                });
            });

            this.uploadArea.addEventListener('drop', (e) => {
                e.preventDefault();
                this.uploadArea.classList.remove('dragover');
                if (e.dataTransfer.files.length) {
                    this.handleFiles(e.dataTransfer.files);
                }
            });
        }

        if (this.fileInput) {
            this.fileInput.addEventListener('change', (e) => {
                if (e.target.files.length) {
                    this.handleFiles(e.target.files);
                }
            });
        }

        if (this.uploadButton) {
            this.uploadButton.addEventListener("click", () => this.uploadFiles());
        }

        if (this.copyButton && this.linkUrl) {
            this.copyButton.addEventListener('click', () => this.copyLinkToClipboard());
        }
    }
    
    handleFiles(files) {
        Array.from(files).forEach(file => {
            if (!this.selectedFiles.some(f => f.name === file.name)) {
                this.selectedFiles.push(file);
                this.addFileToList(file);
            }
        });
        this.updateUploadButton();
    }
    
    addFileToList(file) {
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
            this.selectedFiles = this.selectedFiles.filter(f => f !== file);
            fileItem.remove();
            this.updateUploadButton();
        };
        
        fileItem.appendChild(fileName);
        fileItem.appendChild(fileSize);
        fileItem.appendChild(removeButton);
        this.filesList.appendChild(fileItem);
        this.filesList.style.display = 'block';
    }
    
    updateUploadButton() {
        if (this.uploadButton) {
            this.uploadButton.disabled = this.selectedFiles.length === 0;
        }
    }
    
    resetUploadUI() {
        if (this.statusText) {
            this.statusText.innerText = "Pripremam upload...";
            this.statusText.style.color = "#86868b";
        }
        if (this.linkContainer) {
            this.linkContainer.style.display = "none";
        }
        if (this.uploadButton) {
            this.uploadButton.disabled = true;
        }
        if (this.loaderContainer) {
            this.loaderContainer.style.display = "block";
        }
        if (this.progressFill) {
            this.progressFill.style.width = "0%";
        }
        if (this.percentText) {
            this.percentText.textContent = "0%";
        }
        if (this.uploadSpeed) {
            this.uploadSpeed.textContent = "Računam...";
        }
        this.uploadStartTime = null;
        this.lastUploadedBytes = 0;
        this.lastSpeedUpdateTime = null;
    }
    
    updateProgress(percent, phase) {
        this.uploadPhase = phase;
        this.totalProgress = percent;
        
        if (this.progressFill) {
            this.progressFill.style.width = `${percent}%`;
        }
        
        if (this.percentText) {
            this.percentText.textContent = `${Math.round(percent)}%`;
        }
        
        if (this.statusText) {
            let statusMessage = '';
            switch (phase) {
                case 'preparing':
                    statusMessage = 'Pripremam fajlove...';
                    break;
                case 'uploading':
                    statusMessage = 'Otpremam fajlove...';
                    break;
                case 'transferring':
                    statusMessage = 'Prebacujem na server...';
                    break;
                case 'complete':
                    statusMessage = 'Otpremanje završeno!';
                    break;
            }
            this.statusText.textContent = statusMessage;
        }
    }
    
    async uploadFiles() {
        if (this.selectedFiles.length === 0) {
            if (this.statusText) {
                this.statusText.textContent = "Molimo izaberite fajlove za otpremanje";
                this.statusText.style.color = "#ff3b30";
            }
            return;
        }

        this.resetUploadUI();
        this.updateProgress(0, 'preparing');

        const formData = new FormData();
        this.selectedFiles.forEach(file => {
            formData.append("files", file);
        });

        try {
            const xhr = new XMLHttpRequest();
            xhr.upload.onprogress = (event) => {
                if (event.lengthComputable) {
                    // Upload progress is 80% of total progress
                    const uploadPercent = (event.loaded / event.total) * 80;
                    this.updateProgress(uploadPercent, 'uploading');
                    this.updateSpeed(event);
                }
            };

            xhr.onreadystatechange = () => {
                if (xhr.readyState === 3) {
                    // Transfer to B2 is remaining 20%
                    this.updateProgress(90, 'transferring');
                }
                if (xhr.readyState === 4) {
                    if (xhr.status === 200) {
                        const response = JSON.parse(xhr.responseText);
                        this.handleUploadSuccess(response);
                    } else {
                        this.handleUploadError(xhr.responseText);
                    }
                }
            };

            xhr.open('POST', '/upload', true);
            xhr.send(formData);
        } catch (error) {
            this.handleUploadError(error.message);
        }
    }
    
    updateSpeed(event) {
        const now = Date.now();
        if (!this.uploadStartTime) {
            this.uploadStartTime = now;
            this.lastSpeedUpdateTime = now;
            this.lastUploadedBytes = 0;
        }
        
        const timeDiff = (now - this.lastSpeedUpdateTime) / 1000;
        if (timeDiff >= 1) {
            const bytesDiff = event.loaded - this.lastUploadedBytes;
            const speed = bytesDiff / timeDiff;
            const remainingBytes = event.total - event.loaded;
            const eta = remainingBytes / speed;
            
            if (this.uploadSpeed) {
                this.uploadSpeed.textContent = `${formatSpeed(speed)} • ${formatTime(eta)} preostalo`;
            }
            
            this.lastUploadedBytes = event.loaded;
            this.lastSpeedUpdateTime = now;
        }
    }
    
    handleUploadSuccess(response) {
        this.updateProgress(100, 'complete');
        
        if (this.statusText) {
            this.statusText.textContent = "Otpremanje uspešno!";
            this.statusText.style.color = "#34c759";
            this.statusText.classList.add('success');
        }
        
        if (this.linkUrl) {
            const fullUrl = window.location.origin + "/file/" + response.download_id;
            this.linkUrl.textContent = fullUrl;
        }
        
        if (this.linkContainer) {
            this.linkContainer.style.display = "block";
        }
        
        // Reset form
        this.resetForm();
    }
    
    handleUploadError(error) {
        if (this.statusText) {
            this.statusText.textContent = `Greška: ${error}`;
            this.statusText.style.color = "#ff3b30";
        }
        if (this.loaderContainer) {
            this.loaderContainer.style.display = "none";
        }
        if (this.uploadButton) {
            this.uploadButton.disabled = false;
        }
    }
    
    resetForm() {
        if (this.fileInput) {
            this.fileInput.value = "";
        }
        this.selectedFiles = [];
        if (this.filesList) {
            this.filesList.innerHTML = "";
            this.filesList.style.display = "none";
        }
        
        if (this.uploadButton) {
            this.uploadButton.disabled = false;
        }
    }
    
    copyLinkToClipboard() {
        if (this.linkUrl) {
            navigator.clipboard.writeText(this.linkUrl.textContent)
                .then(() => {
                    this.notification.classList.add('show');
                    setTimeout(() => {
                        this.notification.classList.remove('show');
                    }, 2000);
                })
                .catch(err => {
                    console.error('Failed to copy:', err);
                    // Fallback for older browsers
                    const textArea = document.createElement('textarea');
                    textArea.value = this.linkUrl.textContent;
                    document.body.appendChild(textArea);
                    textArea.select();
                    try {
                        document.execCommand('copy');
                        this.notification.classList.add('show');
                        setTimeout(() => {
                            this.notification.classList.remove('show');
                        }, 2000);
                    } catch (err) {
                        console.error('Fallback failed:', err);
                    }
                    document.body.removeChild(textArea);
                });
        }
    }
}

// Initialize background images
class BackgroundManager {
    constructor(images) {
        this.images = images;
        this.currentImageIndex = 0;
        this.backgroundSection = document.querySelector('.background-section');
        this.backgroundCredit = document.querySelector('.background-credit');
        
        if (this.backgroundSection && this.images.length > 0) {
            this.initialize();
        }
    }
    
    initialize() {
        const firstImage = this.createBackgroundImage(this.images[0]);
        this.backgroundSection.appendChild(firstImage);
        if (this.backgroundCredit) {
            this.backgroundCredit.textContent = this.images[0].credit;
        }
        
        setTimeout(() => {
            this.rotateBackground();
            setInterval(() => this.rotateBackground(), 10000);
        }, 5000);
    }
    
    createBackgroundImage(imageData) {
        const img = document.createElement('img');
        img.src = imageData.url;
        img.className = 'background-image';
        img.onload = () => img.classList.add('active');
        return img;
    }
    
    rotateBackground() {
        if (!this.backgroundSection) return;
        
        const currentImage = document.querySelector('.background-image.active');
        const nextImageData = this.images[this.currentImageIndex];
        const nextImage = this.createBackgroundImage(nextImageData);
        
        this.backgroundSection.appendChild(nextImage);
        
        if (currentImage) {
            currentImage.classList.remove('active');
            setTimeout(() => currentImage.remove(), 500);
        }
        
        if (this.backgroundCredit) {
            this.backgroundCredit.textContent = nextImageData.credit;
        }
        
        this.currentImageIndex = (this.currentImageIndex + 1) % this.images.length;
    }
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
    const uploader = new FileUploader();
    const backgroundManager = new BackgroundManager(window.backgroundImages || []);
}); 