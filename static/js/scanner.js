let scannerStream = null;
let scannerTarget = null;

function openScanner(targetId) {
    scannerTarget = document.getElementById(targetId);

    const modal = document.getElementById("scannerModal");
    if (!modal) {
        alert("Scanner is not available.");
        return;
    }

    modal.style.display = "flex";
    startCamera();
}

async function startCamera() {
    const video = document.getElementById("scannerVideo");
    const result = document.getElementById("scannerResult");

    try {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            result.textContent = "Camera is not supported by this browser.";
            return;
        }

        scannerStream = await navigator.mediaDevices.getUserMedia({
            video: {
                facingMode: "environment",
                width: { ideal: 1280 },
                height: { ideal: 720 }
            },
            audio: false
        });

        video.srcObject = scannerStream;

        video.setAttribute("playsinline", "");
        video.setAttribute("autoplay", "");
        video.muted = true;

        await video.play();

        result.textContent = "?? Camera ready. Point at the number plate.";

    } catch (error) {
        console.error("Camera error:", error);
        result.textContent =
            "? Camera could not start. Allow camera permission and try again.";
    }
}
function capturePlate() {
    const video = document.getElementById("scannerVideo");
    const canvas = document.getElementById("scannerCanvas");
    const result = document.getElementById("scannerResult");

    if (!video || !video.videoWidth) {
        result.textContent = "Camera is not ready yet.";
        return;
    }

    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;

    const ctx = canvas.getContext("2d");
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

    const imageData = canvas.toDataURL("image/jpeg", 0.9);

    recognizePlate(imageData);
}

async function recognizePlate(imageData) {
    const result = document.getElementById("scannerResult");

    result.textContent = "?? Reading number plate...";

    try {
        if (typeof Tesseract === "undefined") {
            result.textContent = "OCR library could not be loaded.";
            return;
        }

        const worker = await Tesseract.createWorker("eng");

        const response = await worker.recognize(imageData);

        await worker.terminate();

        let text = response.data.text
            .toUpperCase()
            .replace(/[^A-Z0-9]/g, "");

        const match = text.match(
            /(?:AP|TS|KA|TN|KL|MH|DL|UP|HR|GJ|RJ|MP|OD|WB|CG|BR|PB|JH|UK|AS|GA)[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}/
        );

        if (match) {
            if (scannerTarget) {
                scannerTarget.value = match[0];
                scannerTarget.dispatchEvent(
                    new Event("input", { bubbles: true })
                );
                scannerTarget.dispatchEvent(
                    new Event("change", { bubbles: true })
                );
            }

            result.textContent = "? Detected: " + match[0];

            setTimeout(() => {
                closeScanner();
            }, 1500);

        } else {
            result.textContent =
                "?? Plate not detected. Keep the plate clear and try again.";
        }

    } catch (error) {
        console.error(error);
        result.textContent =
            "? OCR failed. Please capture the plate again.";
    }
}

function closeScanner() {
    if (scannerStream) {
        scannerStream.getTracks().forEach(track => track.stop());
        scannerStream = null;
    }

    const video = document.getElementById("scannerVideo");

    if (video) {
        video.srcObject = null;
    }

    const modal = document.getElementById("scannerModal");

    if (modal) {
        modal.style.display = "none";
    }

    scannerTarget = null;
}

