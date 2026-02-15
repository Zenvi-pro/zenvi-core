// Director Panel JavaScript

let bridge = null;
let allDirectors = [];
let selectedDirectors = new Set();

// Initialize QWebChannel
new QWebChannel(qt.webChannelTransport, function(channel) {
    bridge = channel.objects.directorPanelBridge;
    console.log("Director Panel Bridge connected");

    // Listen for directors loaded
    bridge.directorsLoaded.connect(function(directorsJson) {
        console.log("Directors loaded:", directorsJson);
        allDirectors = JSON.parse(directorsJson);
        renderDirectors(allDirectors);
    });

    // Request directors on init
    bridge.loadDirectors();
});

// Render directors grid
function renderDirectors(directors) {
    const grid = document.getElementById('directors-grid');

    if (directors.length === 0) {
        grid.innerHTML = '<div class="loading">No directors available</div>';
        return;
    }

    grid.innerHTML = '';

    directors.forEach(director => {
        const card = createDirectorCard(director);
        grid.appendChild(card);
    });
}

// Create director card
function createDirectorCard(director) {
    const card = document.createElement('div');
    card.className = 'director-card';
    card.dataset.id = director.id;

    if (selectedDirectors.has(director.id)) {
        card.classList.add('selected');
    }

    // Tags
    const tagsHtml = director.tags.map(tag =>
        `<span class="tag">${escapeHtml(tag)}</span>`
    ).join('');

    // Expertise
    const expertiseHtml = director.expertise.slice(0, 3).map(exp =>
        `<span class="expertise-badge">${escapeHtml(exp)}</span>`
    ).join('');

    card.innerHTML = `
        <div class="director-header">
            <div class="director-name">${escapeHtml(director.name)}</div>
            <div class="director-author">by ${escapeHtml(director.author)}</div>
        </div>
        <div class="director-description">${escapeHtml(director.description)}</div>
        <div class="director-tags">${tagsHtml}</div>
        <div class="director-expertise">${expertiseHtml}</div>
    `;

    card.addEventListener('click', () => toggleDirector(director.id));

    return card;
}

// Toggle director selection
function toggleDirector(directorId) {
    if (selectedDirectors.has(directorId)) {
        selectedDirectors.delete(directorId);
    } else {
        selectedDirectors.add(directorId);
    }

    // Update UI
    updateSelectionUI();
}

// Update selection UI
function updateSelectionUI() {
    // Update cards
    document.querySelectorAll('.director-card').forEach(card => {
        const id = card.dataset.id;
        if (selectedDirectors.has(id)) {
            card.classList.add('selected');
        } else {
            card.classList.remove('selected');
        }
    });

    // Update count
    const count = selectedDirectors.size;
    document.getElementById('selected-count').textContent =
        count === 0 ? 'None selected' :
        count === 1 ? '1 selected' :
        `${count} selected`;

    // Enable/disable apply button
    document.getElementById('btn-apply').disabled = count === 0;
}

// Filter directors
function filterDirectors() {
    const query = document.getElementById('search-input').value.toLowerCase();

    if (!query) {
        renderDirectors(allDirectors);
        return;
    }

    const filtered = allDirectors.filter(director => {
        return director.name.toLowerCase().includes(query) ||
               director.description.toLowerCase().includes(query) ||
               director.tags.some(tag => tag.toLowerCase().includes(query)) ||
               director.author.toLowerCase().includes(query);
    });

    renderDirectors(filtered);
}

// Apply selection
function applySelection() {
    if (!bridge || selectedDirectors.size === 0) {
        return;
    }

    const selected = Array.from(selectedDirectors);
    console.log("Applying selection:", selected);

    bridge.selectDirectors(JSON.stringify(selected));
}

// Open marketplace
function openMarketplace() {
    if (!bridge) return;
    bridge.openMarketplace();
}

// Utility: Escape HTML
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

console.log("Director Panel UI initialized");
