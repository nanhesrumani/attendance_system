/* ============================================
   AI Attendance System — Main JavaScript
   ============================================ */

document.addEventListener('DOMContentLoaded', function () {

    // ============================================
    // SIDEBAR TOGGLE
    // ============================================
    const sidebar = document.getElementById('sidebar');
    const sidebarToggle = document.getElementById('sidebarToggle');
    const wrapper = document.getElementById('wrapper');

    if (sidebarToggle && sidebar) {
        sidebarToggle.addEventListener('click', function () {
            // Desktop: collapse/expand
            if (window.innerWidth >= 992) {
                sidebar.classList.toggle('collapsed');
                // Save preference
                localStorage.setItem('sidebarCollapsed',
                    sidebar.classList.contains('collapsed') ? '1' : '0');
            } else {
                // Mobile: slide in/out
                sidebar.classList.toggle('show');
                toggleOverlay();
            }
        });

        // Restore sidebar state on desktop
        if (window.innerWidth >= 992) {
            const collapsed = localStorage.getItem('sidebarCollapsed');
            if (collapsed === '1') {
                sidebar.classList.add('collapsed');
            }
        }
    }

    // Mobile overlay
    function toggleOverlay() {
        let overlay = document.querySelector('.sidebar-overlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.className = 'sidebar-overlay';
            document.body.appendChild(overlay);
            overlay.addEventListener('click', function () {
                if (sidebar) {
                    sidebar.classList.remove('show');
            }
                overlay.classList.remove('show');
            });
        }
        if (sidebar && overlay) {
             overlay.classList.toggle('show', sidebar.classList.contains('show'));
        }
    }

    // Close sidebar on mobile when clicking a link
    document.querySelectorAll('.sidebar-nav .nav-link').forEach(function (link) {
        link.addEventListener('click', function () {
            if (window.innerWidth < 992) {
                if (sidebar) {
                    sidebar.classList.remove('show');
                }
                const overlay = document.querySelector('.sidebar-overlay');
                if (overlay) overlay.classList.remove('show');
            }
        });
    });

    // Handle window resize
    window.addEventListener('resize', function () {
        if (window.innerWidth >= 992) {
            if (sidebar) {
                sidebar.classList.remove('show');
            }
            const overlay = document.querySelector('.sidebar-overlay');
            if (overlay) overlay.classList.remove('show');
        }
    });


    // ============================================
    // AUTO-DISMISS ALERTS
    // ============================================
    document.querySelectorAll('.alert:not(.alert-permanent)').forEach(function (alert) {
        setTimeout(function () {
            try {
                const bsAlert = bootstrap.Alert.getOrCreateInstance(alert);
                bsAlert.close();
            } catch (e) {
                alert.style.display = 'none';
            }
        }, 8000); // 8 seconds
    });


    // ============================================
    // TOOLTIPS INIT
    // ============================================
    const tooltipTriggerList = document.querySelectorAll('[data-bs-toggle="tooltip"]');
    tooltipTriggerList.forEach(function (el) {
        new bootstrap.Tooltip(el);
    });


    // ============================================
    // CONFIRM DELETE FORMS
    // ============================================
    document.querySelectorAll('form[data-confirm]').forEach(function (form) {
        form.addEventListener('submit', function (e) {
            const msg = form.getAttribute('data-confirm') || 'Are you sure?';
            if (!confirm(msg)) {
                e.preventDefault();
            }
        });
    });


    // ============================================
    // SEARCH FILTER (Client-side table filter)
    // ============================================
    const searchInput = document.getElementById('tableSearchInput');
    if (searchInput) {
        searchInput.addEventListener('input', function () {
            const query = this.value.toLowerCase();
            const table = document.getElementById('filterableTable');
            if (!table) return;

            const rows = table.querySelectorAll('tbody tr');
            rows.forEach(function (row) {
                const text = row.textContent.toLowerCase();
                row.style.display = text.includes(query) ? '' : 'none';
            });
        });
    }


    // ============================================
    // ACTIVE NAV LINK HIGHLIGHT
    // ============================================
    const currentPath = window.location.pathname;
    document.querySelectorAll('.sidebar-nav .nav-link').forEach(function (link) {
        const href = link.getAttribute('href');
        if (href && currentPath.startsWith(href) && href !== '/') {
            // Remove active from all
            document.querySelectorAll('.sidebar-nav .nav-link.active').forEach(function (el) {
                el.classList.remove('active');
            });
            link.classList.add('active');
        }
    });


    // ============================================
    // DATETIME DISPLAY
    // ============================================
    const clockEl = document.getElementById('liveClock');
    if (clockEl) {
        function updateClock() {
            const now = new Date();
            clockEl.textContent = now.toLocaleTimeString('en-IN', {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: true
            });
        }
        updateClock();
        setInterval(updateClock, 1000);
    }

});


// ============================================
// GLOBAL UTILITY FUNCTIONS
// ============================================

/**
 * Make an AJAX POST request and return JSON
 */
function ajaxPost(url, data) {
    return fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify(data || {})
    }).then(function (response) {
        return response.json();
    });
}

/**
 * Make an AJAX GET request and return JSON
 */
function ajaxGet(url) {
    return fetch(url, {
        headers: {
            'X-Requested-With': 'XMLHttpRequest'
        }
    }).then(function (response) {
        return response.json();
    });
}

/**
 * Show a toast notification
 */
function showToast(message, type) {
    type = type || 'info';
    const colors = {
        'success': '#198754',
        'danger': '#dc3545',
        'warning': '#ffc107',
        'info': '#0dcaf0'
    };

    const toast = document.createElement('div');
    toast.style.cssText =
        'position:fixed; top:20px; right:20px; z-index:9999; ' +
        'padding:12px 20px; border-radius:8px; color:#fff; ' +
        'font-size:0.85rem; font-weight:500; max-width:400px; ' +
        'box-shadow:0 4px 12px rgba(0,0,0,0.15); ' +
        'animation:fadeIn 0.3s ease; ' +
        'background:' + (colors[type] || colors.info);
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(function () {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s ease';
        setTimeout(function () { toast.remove(); }, 300);
    }, 4000);
}

/**
 * Toggle password visibility
 */
function togglePassword(inputId, btn) {
    const input = document.getElementById(inputId);
    if (!input) return;
    const icon = btn.querySelector('i');
    if (input.type === 'password') {
        input.type = 'text';
        if (icon) icon.className = 'bi bi-eye-slash';
    } else {
        input.type = 'password';
        if (icon) icon.className = 'bi bi-eye';
    }
}

/**
 * Format a date string to display format
 */
function formatDate(dateStr) {
    if (!dateStr) return '—';
    const d = new Date(dateStr);
    return d.toLocaleDateString('en-IN', {
        year: 'numeric',
        month: 'short',
        day: 'numeric'
    });
}

/**
 * Format time from seconds or HH:MM:SS
 */
function formatTime(timeStr) {
    if (!timeStr) return '—';
    if (typeof timeStr === 'number') {
        const hours = Math.floor(timeStr / 3600);
        const mins = Math.floor((timeStr % 3600) / 60);
        return hours.toString().padStart(2, '0') + ':' + mins.toString().padStart(2, '0');
    }
    return timeStr;
}

/**
 * Copy text to clipboard
 */
function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(function () {
        showToast('Copied to clipboard!', 'success');
    }).catch(function () {
        // Fallback
        const textarea = document.createElement('textarea');
        textarea.value = text;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        textarea.remove();
        showToast('Copied to clipboard!', 'success');
    });
}

/**
 * Debounce function for search inputs
 */
function debounce(func, wait) {
    let timeout;
    return function () {
        const context = this;
        const args = arguments;
        clearTimeout(timeout);
        timeout = setTimeout(function () {
            func.apply(context, args);
        }, wait);
    };
}

/**
 * Auto-refresh a container via AJAX
 * Usage: autoRefresh('/api/data', '#container', 5000)
 */
function autoRefresh(url, containerSelector, intervalMs) {
    function refresh() {
        fetch(url, {
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        })
        .then(function (r) { return r.text(); })
        .then(function (html) {
            const container = document.querySelector(containerSelector);
            if (container) container.innerHTML = html;
        })
        .catch(function () { /* silent fail */ });
    }

    refresh();
    return setInterval(refresh, intervalMs || 5000);
}

/**
 * Live recognition status poller
 */
function pollRecognitionStatus(sessionId, statusContainerId, intervalMs) {
    const container = document.getElementById(statusContainerId);
    if (!container) return;

    function poll() {
        ajaxGet('/admin/recognition/api/status/' + sessionId)
            .then(function (data) {
                if (data.is_running) {
                    container.innerHTML =
                        '<span class="badge bg-success"><i class="bi bi-broadcast"></i> Live</span> ' +
                        'FPS: ' + (data.stats ? data.stats.fps : '—') + ' | ' +
                        'Present: ' + (data.stats ? data.stats.present_count : '—');
                } else {
                    container.innerHTML =
                        '<span class="badge bg-secondary">Not Running</span>';
                }
            })
            .catch(function () {
                container.innerHTML = '<span class="badge bg-warning">Unknown</span>';
            });
    }

    poll();
    return setInterval(poll, intervalMs || 3000);
}

/**
 * Poll recognition log
 */
function pollRecognitionLog(sessionId, logContainerId, intervalMs) {
    const container = document.getElementById(logContainerId);
    if (!container) return;

    function poll() {
        ajaxGet('/admin/recognition/api/log/' + sessionId + '?n=20')
            .then(function (data) {
                if (!data.log || data.log.length === 0) {
                    container.innerHTML = '<small class="text-muted">No events yet...</small>';
                    return;
                }
                let html = '';
                data.log.reverse().forEach(function (entry) {
                    const color = entry.type === 'present' ? 'success' :
                                  entry.type === 'spoof' ? 'danger' : 'secondary';
                    html += '<div class="small py-1 border-bottom">' +
                        '<span class="text-muted">' + entry.time + '</span> ' +
                        '<span class="badge bg-' + color + '">' + entry.type + '</span> ' +
                        entry.message + '</div>';
                });
                container.innerHTML = html;
                container.scrollTop = 0;
            })
            .catch(function () { /* silent */ });
    }

    poll();
    return setInterval(poll, intervalMs || 2000);
}

    function initModels() {
        const btn = document.getElementById("btnInitModels");
        const loader = document.getElementById("btnLoader");
        const text = document.getElementById("btnText");

        // 🔥 START LOADING
        btn.disabled = true;
        loader.classList.remove("d-none");
        text.innerHTML = "Initializing...";

        fetch("/admin/recognition/init-models", {
            method: "POST"
        })
        .then(res => res.json())
        .then(data => {
            // ✅ SUCCESS UI
            text.innerHTML = "✅ Model Ready";
            btn.classList.remove("btn-outline-primary");
            btn.classList.add("btn-success");

            // optional message
            showAlert(data.message, "success");
        })
        .catch(err => {
            console.error(err);

            text.innerHTML = "❌ Failed";
            btn.classList.remove("btn-outline-primary");
            btn.classList.add("btn-danger");

            showAlert("Initialization failed", "danger");
        })
        .finally(() => {
            loader.classList.add("d-none");
            btn.disabled = false;
        });
    }

    function showAlert(message, type) {
        const container = document.getElementById("alertContainer");
        if (!container) return;

        container.innerHTML = `
            <div class="alert alert-${type} alert-dismissible fade show">
                ${message}
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            </div>
        `;
    }

// ============================================
// WEBCAM UTILITIES (for enrollment)
// ============================================

let _webcamStream = null;

/**
 * Start webcam preview in a video element
 */
function startWebcamPreview(videoElementId) {
    const video = document.getElementById(videoElementId);
    if (!video) return Promise.reject('Video element not found');

    return navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' }
    }).then(function (stream) {
        _webcamStream = stream;
        video.srcObject = stream;
        return stream;
    });
}

/**
 * Stop webcam stream
 */
function stopWebcamPreview() {
    if (_webcamStream) {
        _webcamStream.getTracks().forEach(function (track) { track.stop(); });
        _webcamStream = null;
    }
}

/**
 * Capture frame from webcam as base64 JPEG
 */
function captureWebcamFrame(videoElementId, quality) {
    const video = document.getElementById(videoElementId);
    if (!video) return null;

    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    return canvas.toDataURL('image/jpeg', quality || 0.9);
}