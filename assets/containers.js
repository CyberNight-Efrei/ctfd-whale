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