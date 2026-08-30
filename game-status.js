(() => {
    'use strict';
    const grid = document.getElementById('game-grid');
    const empty = document.getElementById('empty-state');
    const connection = document.getElementById('connection');
    const notice = document.getElementById('notice');
    const previous = document.getElementById('previous-page');
    const next = document.getElementById('next-page');
    const countButtons = [...document.querySelectorAll('.segmented button')];
    let page = 1;
    let pageSize = 4;
    let totalPages = 1;
    let logPages = {};
    let socket = null;
    let retryTimer = null;
    let retryDelay = 1000;
    let lastReceived = Date.now();
    let hasSnapshot = false;
    let stopped = false;
    let revision = 0;
    const cards = new Map();
    const tradeStates = new Map();
    const timeFormat = new Intl.DateTimeFormat('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const startFormat = new Intl.DateTimeFormat('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }
    function setConnection(state, text) {
        connection.dataset.state = state;
        connection.textContent = text;
    }
    function showError(message) {
        setConnection('error', '업데이트 지연');
        notice.textContent = message;
        notice.hidden = false;
        previous.disabled = next.disabled = true;
        if (!hasSnapshot) {
            document.getElementById('empty-title').textContent = '잠시 연결을 기다리고 있어요';
            document.getElementById('empty-description').textContent = '자동으로 다시 연결합니다. 연결되면 현황이 표시됩니다.';
        }
    }
    function subscribe() {
        revision += 1;
        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ page, page_size: pageSize, log_pages: logPages, request_id: revision }));
        }
    }
    function movePage(delta) {
        const target = Math.max(1, Math.min(totalPages, page + delta));
        if (target === page) return;
        page = target;
        logPages = {};
        subscribe();
    }
    previous.addEventListener('click', () => movePage(-1));
    next.addEventListener('click', () => movePage(1));
    countButtons.forEach(button => button.addEventListener('click', () => {
        const selected = Number(button.dataset.count);
        if (pageSize === selected) return;
        pageSize = selected;
        page = 1;
        logPages = {};
        countButtons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
        subscribe();
    }));

    function logButton(game, delta, text) {
        const button = element('button', '', text);
        button.type = 'button';
        button.dataset.logAction = delta < 0 ? 'newer' : 'older';
        button.setAttribute('aria-label', `${game.room_id} ${delta < 0 ? '최신' : '이전'} 로그 4개`);
        button.disabled = delta < 0 ? game.logs.page <= 1 : game.logs.page >= game.logs.total_pages;
        button.addEventListener('click', () => {
            logPages[String(game.id)] = game.logs.page + delta;
            subscribe();
        });
        return button;
    }
    function populateCard(card, game) {
        const focusedAction = card.contains(document.activeElement) ? document.activeElement.dataset.logAction : null;
        card.replaceChildren();
        card.className = `game-card${game.deal ? ' trading' : ''}${game.paused ? ' paused' : ''}`;
        card.setAttribute('aria-label', `${game.room_id} 게임 현황`);
        const heading = element('div', 'card-heading');
        const title = element('div');
        title.append(element('h2', '', `# ${game.room_id}`), element('p', 'card-meta', `GAME ${game.id} · ${startFormat.format(new Date(game.started_at))} 시작`));
        const phase = game.deal?.phase;
        heading.append(title, element('span', 'badge', game.paused ? '연결 대기' : phase === 'BONUS' ? '보너스 선택' : game.deal ? '비밀 거래 중' : '진행 중'));
        const content = element('div', 'card-content');
        const summary = element('div', 'game-summary');
        const turn = element('div', 'turn');
        let label = '현재 차례';
        let name = game.turn ? `${game.turn.nickname} 님` : '참가자 재접속 대기';
        if (phase === 'BONUS') {
            label = '보너스 선택';
            name = game.deal.bonus_name ? `${game.deal.bonus_name} 님` : '참가자 재접속 대기';
        } else if (game.deal) {
            label = '거래 진행';
            name = `${game.deal.initiator_name} ↔ ${game.deal.target_name}`;
        }
        turn.append(element('span', 'turn-label', label), element('span', 'turn-name', name));
        const players = element('div', 'players');
        players.setAttribute('aria-label', '참가자와 완성 줄 수');
        game.players.forEach(player => {
            const current = player.id === game.turn?.id;
            players.append(element('span', `player${current ? ' current' : ''}`, `${current ? '▶ ' : ''}${player.nickname} · ${player.lines}줄${player.connected ? '' : ' · 접속 대기'}`));
        });
        summary.append(turn, players);
        const dangerPlayers = game.players.filter(player => player.lines >= 2 && player.winning_nums.length > 0);
        if (dangerPlayers.length) {
            const danger = element('div', 'danger');
            danger.setAttribute('aria-label', '승리 임박 참가자와 견제 번호');
            dangerPlayers.forEach(player => {
                const row = element('div');
                row.append(element('strong', '', `⚠ ${player.nickname} · 승리 임박 `), document.createTextNode('견제 '));
                player.winning_nums.forEach(number => row.append(element('span', 'danger-number', String(number))));
                danger.append(row);
            });
            summary.append(danger);
        }
        const logs = element('section', 'logs');
        const logHeading = element('div', 'log-heading');
        const nav = element('div', 'log-nav');
        nav.append(logButton(game, -1, '‹'), element('span', '', `${game.logs.page} / ${game.logs.total_pages}`), logButton(game, 1, '›'));
        logHeading.append(element('h3', '', `공개 로그 · ${game.logs.page === 1 ? '최신 ' : ''}4개씩`), nav);
        const list = element('ol', 'log-list');
        game.logs.items.forEach(log => {
            const row = element('li');
            const time = element('time', '', timeFormat.format(new Date(log.time)));
            time.dateTime = log.time;
            row.append(time, element('span', '', log.message));
            list.append(row);
        });
        logs.append(logHeading, list);
        if (!game.logs.items.length) logs.append(element('p', 'log-empty', '새로운 공개 기록을 기다리고 있어요.'));
        content.append(summary, logs);
        card.append(heading, content);
        if (focusedAction) card.querySelector(`[data-log-action="${focusedAction}"]`)?.focus({ preventScroll: true });
    }

    function render(data) {
        hasSnapshot = true;
        notice.hidden = true;
        setConnection('live', '실시간 연결');
        page = data.page;
        totalPages = data.total_pages;
        grid.dataset.count = String(data.page_size);
        document.getElementById('game-total').textContent = String(data.total);
        document.getElementById('page-label').textContent = `${page} / ${totalPages}`;
        previous.disabled = page <= 1;
        next.disabled = page >= totalPages;
        const visibleIds = new Set(data.games.map(game => String(game.id)));
        for (const [id, record] of cards) if (!visibleIds.has(id)) { record.node.remove(); cards.delete(id); }
        for (const id of Object.keys(logPages)) if (!visibleIds.has(id)) delete logPages[id];
        for (const id of tradeStates.keys()) if (!visibleIds.has(id)) tradeStates.delete(id);
        grid.querySelectorAll('.placeholder').forEach(node => node.remove());
        const alerts = [];
        data.games.forEach((game, index) => {
            const id = String(game.id);
            let record = cards.get(id);
            if (!record) { record = { node: element('article', 'game-card'), signature: '' }; cards.set(id, record); }
            const signature = JSON.stringify(game);
            if (signature !== record.signature) {
                populateCard(record.node, game);
                record.signature = signature;
            }
            const tradeKey = game.deal ? `${game.deal.phase}:${game.deal.initiator_name}:${game.deal.target_name}` : '';
            const previousTrade = tradeStates.get(id);
            const recentTrade = previousTrade && game.trade_event_id > previousTrade.eventId;
            if ((tradeKey && previousTrade?.key !== tradeKey) || recentTrade) {
                record.node.classList.add('trade-alert');
                alerts.push(`${game.room_id} 방 ${game.deal?.phase === 'BONUS' ? '보너스 선택 중' : '거래 시작'}`);
            }
            tradeStates.set(id, { key: tradeKey, eventId: game.trade_event_id });
            if (grid.children[index] !== record.node) grid.insertBefore(record.node, grid.children[index] || null);
        });
        if (alerts.length) document.getElementById('announcer').textContent = alerts.join('. ');
        grid.hidden = data.games.length === 0;
        empty.hidden = data.games.length !== 0;
        if (!data.games.length) {
            document.getElementById('empty-title').textContent = data.total ? '게임 목록을 갱신하고 있어요' : '아직 진행 중인 게임이 없어요';
            document.getElementById('empty-description').textContent = '게임이 시작되면 여기에 자동으로 표시됩니다. 누구나 가입 없이 관전할 수 있어요.';
        } else {
            for (let i = data.games.length; i < data.page_size; i++) {
                const placeholder = element('div', 'game-card placeholder');
                placeholder.setAttribute('aria-hidden', 'true');
                placeholder.append(element('span', '', '+'), element('div', '', '다음 게임을 기다리고 있어요'));
                grid.append(placeholder);
            }
        }
    }

    function connect() {
        if (stopped) return;
        clearTimeout(retryTimer);
        setConnection('', '연결 중');
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const current = new WebSocket(`${protocol}//${location.host}/api/game-status/ws`);
        socket = current;
        lastReceived = Date.now();
        current.onopen = () => { if (socket === current) subscribe(); };
        current.onmessage = event => {
            if (socket !== current) return;
            lastReceived = Date.now();
            let data;
            try { data = JSON.parse(event.data); } catch { showError('현황 데이터를 읽지 못했습니다. 다시 연결합니다.'); current.close(); return; }
            if (data.type === 'error') { showError(data.message); return; }
            if (data.type !== 'game_status' || data.request_id !== revision) return;
            retryDelay = 1000;
            render(data);
        };
        current.onerror = () => { if (socket === current) showError('서버 연결이 원활하지 않습니다. 자동으로 다시 연결합니다.'); };
        current.onclose = () => {
            if (socket !== current || stopped) return;
            showError(hasSnapshot ? '연결이 끊겼습니다. 표시된 정보는 마지막 수신 상태이며, 자동으로 재연결합니다.' : '서버에 연결할 수 없습니다. 자동으로 다시 시도합니다.');
            retryTimer = setTimeout(connect, retryDelay);
            retryDelay = Math.min(retryDelay * 2, 10000);
        };
    }
    function checkConnection() {
        if (!stopped && socket && socket.readyState !== WebSocket.CLOSED && Date.now() - lastReceived > 15000) {
            showError('새 현황을 받지 못해 다시 연결합니다.');
            socket.close();
        }
    }
    let watchdog = setInterval(checkConnection, 5000);
    window.addEventListener('pagehide', () => { stopped = true; clearTimeout(retryTimer); clearInterval(watchdog); socket?.close(); });
    window.addEventListener('pageshow', event => { if (event.persisted) { stopped = false; watchdog = setInterval(checkConnection, 5000); connect(); } });
    connect();
})();
