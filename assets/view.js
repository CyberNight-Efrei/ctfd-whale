CTFd._internal.challenge.data = undefined

CTFd._internal.challenge.renderer = null;

CTFd._internal.challenge.preRender = function () {
}

CTFd._internal.challenge.render = null;

CTFd._internal.challenge.postRender = function () {
    loadInfo();
}

if (window.$ === undefined) window.$ = CTFd.lib.$;

function updateContainerStatusBadge(state) {
    var statuses = {
        not_started: { label: "Not launched", tone: "secondary", bootstrap: "bg-secondary badge-secondary" },
        provisioning: { label: "Provisioning", tone: "info", bootstrap: "bg-info badge-info" },
        starting: { label: "Starting", tone: "info", bootstrap: "bg-info badge-info" },
        ready: { label: "Running", tone: "success", bootstrap: "bg-success badge-success" },
        running: { label: "Running", tone: "success", bootstrap: "bg-success badge-success" },
        stopping: { label: "Stopping", tone: "warning", bootstrap: "bg-warning badge-warning" },
        cleanup_pending: { label: "Cleanup pending", tone: "warning", bootstrap: "bg-warning badge-warning" },
        stopped: { label: "Stopped", tone: "secondary", bootstrap: "bg-secondary badge-secondary" },
        degraded: { label: "Degraded", tone: "warning", bootstrap: "bg-warning badge-warning" },
        error: { label: "Error", tone: "danger", bootstrap: "bg-danger badge-danger" },
        missing: { label: "Missing", tone: "danger", bootstrap: "bg-danger badge-danger" },
        unknown: { label: "Unknown", tone: "secondary", bootstrap: "bg-secondary badge-secondary" }
    };
    var status = statuses[state] || statuses.unknown;
    var badges = document.querySelectorAll(".whale-container-status-badge");
    var statusClasses = [
        "whale-status-secondary", "whale-status-info",
        "whale-status-success", "whale-status-warning",
        "whale-status-danger", "bg-secondary", "bg-info",
        "bg-success", "bg-warning", "bg-danger",
        "badge-secondary", "badge-info", "badge-success",
        "badge-warning", "badge-danger"
    ];

    badges.forEach(function (badge) {
        statusClasses.forEach(function (className) {
            badge.classList.remove(className);
        });
        badge.classList.add("whale-status-" + status.tone);
        status.bootstrap.split(" ").forEach(function (className) {
            badge.classList.add(className);
        });
        badge.textContent = status.label;
    });
}

function loadInfo() {
    var challenge_id = CTFd._internal.challenge.data.id;
    var url = "/api/v1/plugins/ctfd-whale/container?challenge_id=" + challenge_id;

    CTFd.fetch(url, {
        method: 'GET',
        credentials: 'same-origin',
        headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
    }).then(function (response) {
        if (response.status === 429) {
            // User was ratelimited but process response
            return response.json();
        }
        if (response.status === 403) {
            // User is not logged in or CTF is paused.
            return response.json();
        }
        return response.json();
    }).then(function (response) {
        if (window.t !== undefined) {
            clearInterval(window.t);
            window.t = undefined;
        }
        if (!response || response.success !== true) {
            // Never leave controls from a previously opened challenge visible
            // when this challenge's instance status cannot be loaded.
            updateContainerStatusBadge("unknown");
            $('#whale-panel-started').hide();
            $('#whale-panel-transition').hide();
            $('#whale-panel-stopped').show();
            if (window.whaleLifecyclePoll !== undefined) {
                clearInterval(window.whaleLifecyclePoll);
                window.whaleLifecyclePoll = undefined;
            }
            CTFd._functions.events.eventAlert({
                title: "Fail",
                html: response && response.message ? response.message : "Unable to retrieve the container status.",
                button: "OK"
            });
            return;
        }

        response = response.data || {};
        if (!response.lifecycle_state) {
            updateContainerStatusBadge("not_started");
            if (window.whaleLifecyclePoll !== undefined) {
                clearInterval(window.whaleLifecyclePoll);
                window.whaleLifecyclePoll = undefined;
            }
            $('#whale-panel-started').hide();
            $('#whale-panel-transition').hide();
            $('#whale-panel-stopped').show();
            return;
        }

        if (response.lifecycle_state !== "ready") {
            updateContainerStatusBadge(response.lifecycle_state);
            const lifecycleMessages = {
                provisioning: "Creating Docker services…",
                starting: "Configuring access routes…",
                stopping: "Stopping this instance…",
                cleanup_pending: "Cleanup is incomplete. Whale will retry automatically.",
                unknown: "The instance state is unknown. Checking again shortly."
            };
            $('#whale-panel-started').hide();
            $('#whale-panel-stopped').hide();
            $('#whale-panel-transition').show();
            $('#whale-lifecycle-message').text(
                lifecycleMessages[response.lifecycle_state] ||
                "The instance is changing state. Checking again shortly."
            );
            if (window.whaleLifecyclePoll === undefined) {
                window.whaleLifecyclePoll = setInterval(loadInfo, 3000);
            }
            return;
        }

        if (window.whaleLifecyclePoll !== undefined) {
            clearInterval(window.whaleLifecyclePoll);
            window.whaleLifecyclePoll = undefined;
        }
        $('#whale-panel-transition').hide();
        var runtimeStatus = response.container_status && response.container_status.state
            ? response.container_status.state
            : "running";
        updateContainerStatusBadge(runtimeStatus);

        if (response.remaining_time != undefined) {
            $('#whale-challenge-user-access').html(response.user_access);
            $('#whale-challenge-lan-domain').text(response.lan_domain);
            $('#whale-challenge-count-down').text(response.remaining_time);
            $('#whale-panel-stopped').hide();
            $('#whale-panel-started').show();

            window.t = setInterval(() => {
                const c = $('#whale-challenge-count-down').text();
                if (!c) return;
                let second = parseInt(c) - 1;
                if (second <= 0) {
                    loadInfo();
                }
                $('#whale-challenge-count-down').text(second);
            }, 1000);
        } else {
            $('#whale-panel-started').hide();
            $('#whale-panel-stopped').show();
        }
    });
};

CTFd._internal.challenge.destroy = function () {
    var challenge_id = CTFd._internal.challenge.data.id;
    var url = "/api/v1/plugins/ctfd-whale/container?challenge_id=" + challenge_id;

    $('#whale-button-destroy').text("Waiting...");
    $('#whale-button-destroy').prop('disabled', true);

    var params = {};

    CTFd.fetch(url, {
        method: 'DELETE',
        credentials: 'same-origin',
        headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(params)
    }).then(function (response) {
        if (response.status === 429) {
            // User was ratelimited but process response
            return response.json();
        }
        if (response.status === 403) {
            // User is not logged in or CTF is paused.
            return response.json();
        }
        return response.json();
    }).then(function (response) {
        if (response.success) {
            loadInfo();
            CTFd._functions.events.eventAlert({
                title: "Success",
                html: "Your instance has been destroyed!",
                button: "OK"
            });
        } else {
            CTFd._functions.events.eventAlert({
                title: "Fail",
                html: response.message,
                button: "OK"
            });
        }
    }).finally(() => {
        $('#whale-button-destroy').text("Destroy this instance");
        $('#whale-button-destroy').prop('disabled', false);
    });
};

CTFd._internal.challenge.renew = function () {
    var challenge_id = CTFd._internal.challenge.data.id;
    var url = "/api/v1/plugins/ctfd-whale/container?challenge_id=" + challenge_id;

    $('#whale-button-renew').text("Waiting...");
    $('#whale-button-renew').prop('disabled', true);

    var params = {};

    CTFd.fetch(url, {
        method: 'PATCH',
        credentials: 'same-origin',
        headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(params)
    }).then(function (response) {
        if (response.status === 429) {
            // User was ratelimited but process response
            return response.json();
        }
        if (response.status === 403) {
            // User is not logged in or CTF is paused.
            return response.json();
        }
        return response.json();
    }).then(function (response) {
        if (response.success) {
            loadInfo();
            CTFd._functions.events.eventAlert({
                title: "Success",
                html: "Your instance has been renewed!",
                button: "OK"
            });
        } else {
            CTFd._functions.events.eventAlert({
                title: "Fail",
                html: response.message,
                button: "OK"
            });
        }
    }).finally(() => {
        $('#whale-button-renew').text("Renew this instance");
        $('#whale-button-renew').prop('disabled', false);
    });
};

CTFd._internal.challenge.boot = function () {
    var challenge_id = CTFd._internal.challenge.data.id;
    var url = "/api/v1/plugins/ctfd-whale/container?challenge_id=" + challenge_id;

    $('#whale-button-boot').text("Waiting...");
    $('#whale-button-boot').prop('disabled', true);

    var params = {};

    CTFd.fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(params)
    }).then(function (response) {
        if (response.status === 429) {
            // User was ratelimited but process response
            return response.json();
        }
        if (response.status === 403) {
            // User is not logged in or CTF is paused.
            return response.json();
        }
        return response.json();
    }).then(function (response) {
        if (response.success) {
            loadInfo();
            CTFd._functions.events.eventAlert({
                title: "Success",
                html: "Your instance has been deployed!",
                button: "OK"
            });
        } else {
            CTFd._functions.events.eventAlert({
                title: "Fail",
                html: response.message,
                button: "OK"
            });
        }
    }).finally(() => {
        $('#whale-button-boot').text("Launch an instance");
        $('#whale-button-boot').prop('disabled', false);
    });
};


CTFd._internal.challenge.submit = function (preview) {
    var challenge_id = CTFd._internal.challenge.data.id;
    var submission = $('#challenge-input').val()

    var body = {
        'challenge_id': challenge_id,
        'submission': submission,
    }
    var params = {}
    if (preview)
        params['preview'] = true

    return CTFd.api.post_challenge_attempt(params, body).then(function (response) {
        if (response.status === 429) {
            // User was ratelimited but process response
            return response
        }
        if (response.status === 403) {
            // User is not logged in or CTF is paused.
            return response
        }
        return response
    })
};
