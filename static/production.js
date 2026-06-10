let historyPage = 1;

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    })[character]);
}

function formatDate(value) {
    return value ? new Date(value).toLocaleString() : 'Not available';
}

async function loadDashboard() {
    const status = document.getElementById('dashboardStatus');
    try {
        const response = await fetch('/api/statistics');
        const data = await response.json();
        if (!response.ok) throw new Error(data.error);
        document.getElementById('statTotal').textContent = data.total;
        document.getElementById('statReal').textContent = data.real;
        document.getElementById('statFake').textContent = data.fake;
        document.getElementById('statConfidence').textContent = `${data.average_confidence}%`;
        document.getElementById('activityList').innerHTML = data.recent_activity.length
            ? data.recent_activity.map(item => `<div class="activity-row"><span>${escapeHtml(item.date)}</span><strong>${item.count} detection${item.count === 1 ? '' : 's'}</strong></div>`).join('')
            : '<div class="empty-state">No recent detection activity yet.</div>';
        status.textContent = '';
    } catch (error) {
        status.textContent = error.message || 'Dashboard statistics are temporarily unavailable.';
    }
}

async function loadHistory(page = 1) {
    historyPage = page;
    const params = new URLSearchParams({
        page,
        search: document.getElementById('historySearch').value,
        result: document.getElementById('historyResult').value,
        model: document.getElementById('historyModel').value,
        sort_by: document.getElementById('historySort').value
    });
    const body = document.getElementById('historyBody');
    body.innerHTML = '<tr><td colspan="6" class="empty-state">Loading detection history...</td></tr>';
    try {
        const response = await fetch(`/api/history?${params}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error);
        const modelSelect = document.getElementById('historyModel');
        if (modelSelect.options.length === 1) {
            data.models.forEach(model => modelSelect.add(new Option(model, model)));
        }
        body.innerHTML = data.records.length
            ? data.records.map(record => `
                <tr>
                    <td>${escapeHtml(formatDate(record.created_at))}</td>
                    <td>${escapeHtml(record.original_filename)}</td>
                    <td><span class="history-badge ${escapeHtml(record.result)}">${escapeHtml(record.result.toUpperCase())}</span></td>
                    <td>${Number(record.confidence).toFixed(2)}%</td>
                    <td>${escapeHtml(record.model_used)}</td>
                    <td><button class="small-action" onclick="showHistoryDetail('${record.id}')"><i class="fas fa-eye"></i> Details</button></td>
                </tr>`).join('')
            : '<tr><td colspan="6" class="empty-state">No matching detections found.</td></tr>';
        document.getElementById('historyPageLabel').textContent = `Page ${data.page} of ${data.pages}`;
        document.getElementById('historyPrevious').disabled = data.page <= 1;
        document.getElementById('historyNext').disabled = data.page >= data.pages;
    } catch (error) {
        body.innerHTML = `<tr><td colspan="6" class="empty-state">${escapeHtml(error.message || 'Detection history is temporarily unavailable.')}</td></tr>`;
    }
}

async function showHistoryDetail(recordId) {
    const panel = document.getElementById('historyDetail');
    try {
        const response = await fetch(`/api/history/${recordId}`);
        const record = await response.json();
        if (!response.ok) throw new Error(record.error);
        const rows = [
            ['Detection Result', record.result.toUpperCase()],
            ['Confidence', `${Number(record.confidence).toFixed(2)}%`],
            ['Date and Time', formatDate(record.created_at)],
            ['Model Used', record.model_used],
            ['Uploaded File', record.original_filename],
            ['File Type', record.file_type || 'Not available'],
            ['Image Dimensions', `${record.image_width} x ${record.image_height} px`],
            ['SHA-256 Hash', record.file_hash]
        ];
        panel.innerHTML = `<h3><i class="fas fa-file-alt"></i> Detection Details</h3>
            ${rows.map(row => `<div class="history-detail-row"><span>${escapeHtml(row[0])}</span><strong>${escapeHtml(row[1])}</strong></div>`).join('')}
            <a class="report-btn" href="/reports/${record.id}.pdf"><i class="fas fa-file-pdf"></i> Download PDF Report</a>`;
        panel.style.display = 'block';
        panel.scrollIntoView({ behavior: 'smooth' });
    } catch (error) {
        panel.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
        panel.style.display = 'block';
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('historyApply').addEventListener('click', () => loadHistory(1));
    document.getElementById('historyPrevious').addEventListener('click', () => loadHistory(historyPage - 1));
    document.getElementById('historyNext').addEventListener('click', () => loadHistory(historyPage + 1));
});
