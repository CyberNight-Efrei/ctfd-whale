const $ = CTFd.lib.$;

function htmlentities(str) {
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function htmlToPlainText(markup) {
    const wrapper = document.createElement("div");
    wrapper.innerHTML = String(markup || "");
    wrapper.querySelectorAll("script, style").forEach((element) => element.remove());
    wrapper.querySelectorAll("a[href]").forEach((link) => {
        const href = link.getAttribute("href");
        const label = link.textContent.trim();
        if (href && label && (href.startsWith("http://") || href.startsWith("https://")) && !label.includes(href)) {
            link.textContent = label + " (" + href + ")";
        }
    });
    wrapper.querySelectorAll("br, hr").forEach((element) => {
        element.insertAdjacentText("beforebegin", "\n");
        element.remove();
    });
    wrapper.querySelectorAll("td:not(:last-child), th:not(:last-child)").forEach((element) => {
        element.insertAdjacentText("afterend", " — ");
    });
    wrapper.querySelectorAll("li, tr, p, h1, h2, h3, h4, h5, h6").forEach((element) => {
        element.insertAdjacentText("beforebegin", "\n");
        element.insertAdjacentText("afterend", "\n");
    });
    return wrapper.textContent
        .replace(/\u00a0/g, " ")
        .replace(/[ \t]+/g, " ")
        .replace(/ *\n */g, "\n")
        .replace(/\n{2,}/g, "\n")
        .trim();
}

async function writeClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch (error) {
            // Clipboard API can be unavailable outside a secure context.
        }
    }

    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    field.style.pointerEvents = "none";
    document.body.appendChild(field);
    field.select();
    let copied = false;
    try {
        copied = document.execCommand("copy");
    } catch (error) {
        copied = false;
    }
    field.remove();
    return copied;
}

async function copyToClipboard(button, value) {
    const text = String(value == null ? "" : value).trim();
    if (!text) return;

    const copied = await writeClipboard(text);
    const target = $(button);
    target.tooltip("dispose");
    target.tooltip({
        title: copied ? "Copied!" : "Copy failed",
        trigger: "manual",
        placement: "top"
    });
    target.tooltip("show");

    if (button._whaleCopyTimer) {
        clearTimeout(button._whaleCopyTimer);
    }
    button._whaleCopyTimer = setTimeout(function () {
        target.tooltip("hide");
    }, 1600);
}

$(document).on("click", ".click-copy", async function (event) {
    event.preventDefault();
    let value;
    if (this.hasAttribute("data-copy-html")) {
        value = htmlToPlainText(this.getAttribute("data-copy-html"));
    } else if (this.hasAttribute("data-copy")) {
        value = this.getAttribute("data-copy");
    } else {
        // Also supports copyable commands rendered inside access details.
        value = this.textContent;
    }
    await copyToClipboard(this, value);
});


async function load_container_logs(container_id) {
    const tail = Number($("#whale-container-logs-tail").val()) || 200;
    const content = $("#whale-container-logs-content");
    const status = $("#whale-container-logs-status");
    const refreshButton = $("#whale-container-logs-refresh");

    content.empty();
    status.text("Loading the last " + tail + " lines per service…");
    refreshButton.prop("disabled", true);

    try {
        const url = "/api/v1/plugins/ctfd-whale/admin/container/"
            + encodeURIComponent(container_id)
            + "/logs?tail=" + encodeURIComponent(tail);
        const response = await CTFd.fetch(url, {
            method: "GET",
            credentials: "same-origin",
            headers: { Accept: "application/json" }
        });
        const result = await response.json();

        if (!response.ok || !result.success) {
            throw new Error(result.message || "Unable to load container logs.");
        }

        const services = (result.data && result.data.services) || [];
        if (services.length === 0) {
            content.append(
                $("<p>").addClass("text-muted").text(
                    "No Docker services were found for this instance. It may already have been removed."
                )
            );
        } else {
            services.forEach(function (service) {
                const section = $("<section>").addClass("whale-log-service");
                section.append($("<h6>").text(service.name || "Docker service"));
                if (service.error) {
                    section.append(
                        $("<div>").addClass("alert alert-warning py-2").text(service.error)
                    );
                } else if (!service.logs) {
                    section.append(
                        $("<pre>").addClass("whale-log-output").text("(No log output returned.)")
                    );
                } else {
                    section.append(
                        $("<pre>").addClass("whale-log-output").text(service.logs)
                    );
                }
                content.append(section);
            });
        }
        status.text("Showing up to " + result.data.tail + " lines per service.");
    } catch (error) {
        status.text(error.message || "An error occurred while loading logs.");
    } finally {
        refreshButton.prop("disabled", false);
    }
}

$(document).on("click", ".view-container-logs", async function () {
    const container_id = $(this).attr("data-container-id");
    if (!container_id) return;

    $("#whale-container-logs-title").text("Logs — instance #" + container_id);
    $("#whale-container-logs-refresh").attr("data-container-id", container_id);
    $("#whale-container-logs-modal").modal("show");
    await load_container_logs(container_id);
});

$("#whale-container-logs-refresh").on("click", async function () {
    const container_id = $(this).attr("data-container-id");
    if (container_id) await load_container_logs(container_id);
});

$("#whale-container-logs-tail").on("change", async function () {
    const container_id = $("#whale-container-logs-refresh").attr("data-container-id");
    if (container_id) await load_container_logs(container_id);
});

async function delete_container(user_id) {
    let response = await CTFd.fetch("/api/v1/plugins/ctfd-whale/admin/container?user_id=" + user_id, {
        method: "DELETE",
        credentials: "same-origin",
        headers: {
            Accept: "application/json",
            "Content-Type": "application/json"
        }
    });
    response = await response.json();
    return response.success;
}
async function renew_container(user_id) {
    let response = await CTFd.fetch(
        "/api/v1/plugins/ctfd-whale/admin/container?user_id=" + user_id, {
        method: "PATCH",
        credentials: "same-origin",
        headers: {
            Accept: "application/json",
            "Content-Type": "application/json"
        }
    });
    response = await response.json();
    return response.success;
}

$('#containers-renew-button').click(function (e) {
    let users = $("input[data-user-id]:checked").map(function () {
        return $(this).data("user-id");
    });
    CTFd.ui.ezq.ezQuery({
        title: "Renew instances",
        body: `Are you sure you want to renew the selected ${users.length} instance(s)?`,
        success: async function () {
            await Promise.all(users.toArray().map((user) => renew_container(user)));
            location.reload();
        }
    });
});

$('#containers-delete-button').click(function (e) {
    let users = $("input[data-user-id]:checked").map(function () {
        return $(this).data("user-id");
    });
    CTFd.ui.ezq.ezQuery({
        title: "Stop instances",
        body: `Are you sure you want to stop the selected ${users.length} instance(s)?`,
        success: async function () {
            await Promise.all(users.toArray().map((user) => delete_container(user)));
            location.reload();
        }
    });
});

$(".delete-container").click(function (e) {
    e.preventDefault();
    let container_id = $(this).attr("container-id");
    let user_id = $(this).attr("user-id");

    CTFd.ui.ezq.ezQuery({
        title: "Stop instance",
        body: "<span>Are you sure you want to stop <strong>instance #{0}</strong>?</span>".format(
            htmlentities(container_id)
        ),
        success: async function () {
            await delete_container(user_id);
            location.reload();
        }
    });
});

$(".renew-container").click(function (e) {
    e.preventDefault();
    let container_id = $(this).attr("container-id");
    let user_id = $(this).attr("user-id");

    CTFd.ui.ezq.ezQuery({
        title: "Renew instance",
        body: "<span>Are you sure you want to renew <strong>instance #{0}</strong>?</span>".format(
            htmlentities(container_id)
        ),
        success: async function () {
            await renew_container(user_id);
            location.reload();
        },
    });
});