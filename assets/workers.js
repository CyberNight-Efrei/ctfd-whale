const $ = CTFd.lib.$;

let currentLogSource = null;

async function loadFrpLogs() {
    if (!currentLogSource) return;
    const tail = Number($("#whale-frp-logs-tail").val()) || 200;
    const status = $("#whale-frp-logs-status");
    const output = $("#whale-frp-logs-output");
    const refresh = $("#whale-frp-logs-refresh");
    status.text("Chargement des " + tail + " dernières lignes…");
    output.text("");
    refresh.prop("disabled", true);

    try {
        const query = new URLSearchParams({
            source: currentLogSource.source,
            id: currentLogSource.id,
            tail: String(tail)
        });
        const response = await CTFd.fetch(
            "/api/v1/plugins/ctfd-whale/admin/cluster/logs?" + query.toString(),
            {
                method: "GET",
                credentials: "same-origin",
                headers: { Accept: "application/json" }
            }
        );
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.message || "Impossible de récupérer les logs.");
        }
        output.text(result.data.logs || "(Aucun log retourné.)");
        status.text("Jusqu’à " + result.data.tail + " lignes affichées.");
    } catch (error) {
        status.text(error.message || "Erreur pendant la récupération des logs.");
    } finally {
        refresh.prop("disabled", false);
    }
}

$(document).on("click", ".whale-view-frp-logs", async function () {
    currentLogSource = {
        source: $(this).attr("data-source"),
        id: $(this).attr("data-source-id")
    };
    $("#whale-frp-logs-title").text("Logs — " + ($(this).attr("data-source-name") || "FRP"));
    $("#whale-frp-logs-modal").modal("show");
    await loadFrpLogs();
});

$("#whale-frp-logs-refresh").on("click", loadFrpLogs);
$("#whale-frp-logs-tail").on("change", loadFrpLogs);
$("#whale-workers-refresh").on("click", function () {
    location.reload();
});
