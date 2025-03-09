class DownloadManager {
    constructor(files) {
        this.files = files;
        this.downloadAllButton = document.getElementById('downloadAllButton');
        this.downloadProgress = document.getElementById('downloadProgress');
        this.progressFill = document.getElementById('progressFill');
        this.progressText = document.getElementById('progressText');
        
        if (this.downloadAllButton) {
            this.downloadAllButton.addEventListener('click', (e) => {
                e.preventDefault();
                this.downloadAllFiles();
            });
        }
    }
    
    async downloadAllFiles() {
        this.downloadAllButton.style.display = 'none';
        this.downloadProgress.style.display = 'block';
        
        let downloadedCount = 0;
        
        for (const file of this.files) {
            this.progressText.textContent = `Preuzimanje ${file.filename}...`;
            this.progressFill.style.width = '0%';
            
            try {
                const response = await fetch(file.download_url);
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
                this.progressFill.style.width = `${(downloadedCount / this.files.length) * 100}%`;
                this.progressText.textContent = `Preuzeto ${downloadedCount} od ${this.files.length} fajlova`;
            } catch (error) {
                console.error('Greška pri preuzimanju fajla:', error);
                this.progressText.textContent = `Greška pri preuzimanju ${file.filename}. Nastavljam sa sledećim...`;
            }
        }
        
        this.progressText.textContent = 'Svi fajlovi preuzeti!';
        setTimeout(() => {
            this.downloadAllButton.style.display = 'block';
            this.downloadProgress.style.display = 'none';
        }, 2000);
    }
}

// Background image management
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
    const downloadManager = new DownloadManager(window.downloadFiles || []);
    const backgroundManager = new BackgroundManager(window.backgroundImages || []);
}); 