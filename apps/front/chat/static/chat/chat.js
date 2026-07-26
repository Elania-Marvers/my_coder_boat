/*
 * FICHIER :
 * apps/front/chat/static/chat/chat.js
 *
 * RÔLE :
 * Gère les interactions du chat dans le navigateur :
 * envoi AJAX, affichage des états, polling et reprise d'un job.
 *
 * APPELLE :
 * - POST /jobs/submit/
 *   → apps/front/chat/views.py::submit_job()
 *
 * - GET /jobs/<job_id>/
 *   → apps/front/chat/views.py::job_status()
 *
 * PIPELINES :
 * - CHAT_JOB_CREATE
 * - CHAT_JOB_STATUS
 * - CHAT_JOB_COMPLETE
 * - CHAT_JOB_FAILURE
 */


/*
 * RÔLE :
 * Regroupe les éléments HTML utilisés par les fonctions du fichier.
 *
 * FOURNIS PAR :
 * apps/front/chat/templates/chat/index.html
 */
const appRoot = document.querySelector("#app-root");
const form = document.querySelector("#chat-form");
const textarea = document.querySelector(".composer__input");
const sendButton = document.querySelector("#send-button");
const conversation = document.querySelector("#conversation");

const jobStatus = document.querySelector("#job-status");
const jobStatusTitle = document.querySelector("#job-status-title");
const jobStatusId = document.querySelector("#job-status-id");
const jobStatusMessage = document.querySelector("#job-status-message");

const jobStatusProgress = document.querySelector(
    "#job-status-progress"
);

const jobStatusProgressBar = document.querySelector(
    "#job-status-progress-bar"
);

const clientError = document.querySelector("#client-error");

const clientErrorMessage = document.querySelector(
    "#client-error-message"
);


/*
 * RÔLE :
 * Place la conversation sur son message le plus récent.
 *
 * APPELÉE PAR :
 * - initialiseChatPage()
 *
 * APPELLE :
 * - Aucun service externe.
 *
 * PIPELINE :
 * - CHAT_PAGE_DISPLAY
 */
function scrollConversation() {
    if (!conversation) {
        return;
    }

    conversation.scrollTop =
        conversation.scrollHeight;
}


/*
 * RÔLE :
 * Adapte automatiquement la hauteur du champ
 * à la quantité de texte saisie.
 *
 * APPELÉE PAR :
 * - initialiseChatPage()
 * - Événement input du textarea
 * - bindSuggestionButtons()
 *
 * APPELLE :
 * - Aucun service externe.
 *
 * PIPELINE :
 * - CHAT_PAGE_DISPLAY
 */
function resizeTextarea() {
    if (!textarea) {
        return;
    }

    textarea.style.height = "auto";

    textarea.style.height =
        Math.min(
            textarea.scrollHeight,
            180
        ) + "px";
}


/*
 * RÔLE :
 * Bloque ou réactive le formulaire pendant
 * la création et le traitement d'un job.
 *
 * APPELÉE PAR :
 * - submitJob()
 * - resumeActiveJob()
 *
 * MODIFIE :
 * - textarea.readOnly
 * - textarea aria-busy
 * - sendButton.disabled
 * - texte du bouton
 *
 * PIPELINES :
 * - CHAT_JOB_CREATE
 * - CHAT_JOB_STATUS
 */
function setFormBusy(isBusy) {
    if (textarea) {
        /*
         * readOnly empêche l'édition sans retirer
         * le champ des données du formulaire.
         */
        textarea.readOnly = isBusy;

        textarea.setAttribute(
            "aria-busy",
            String(isBusy)
        );
    }

    if (!sendButton) {
        return;
    }

    sendButton.disabled = isBusy;

    const label = sendButton.querySelector(
        ".send-button__label"
    );

    if (label) {
        label.textContent = isBusy
            ? "En attente…"
            : "Envoyer";
    }
}


/*
 * RÔLE :
 * Affiche une erreur dans la zone prévue par le template.
 *
 * APPELÉE PAR :
 * - submitJob()
 * - resumeActiveJob()
 *
 * MODIFIE :
 * - #client-error
 * - #client-error-message
 *
 * PIPELINE :
 * - CHAT_JOB_FAILURE
 */
function showError(message) {
    if (
        !clientError
        || !clientErrorMessage
    ) {
        return;
    }

    clientErrorMessage.textContent = message;
    clientError.hidden = false;
}


/*
 * RÔLE :
 * Masque l'erreur précédemment affichée.
 *
 * APPELÉE PAR :
 * - submitJob()
 *
 * PIPELINE :
 * - CHAT_JOB_CREATE
 */
function clearError() {
    if (clientError) {
        clientError.hidden = true;
    }
}


/*
 * RÔLE :
 * Met à jour la carte visuelle du job à partir
 * de l'état JSON retourné par Django.
 *
 * APPELÉE PAR :
 * - submitJob()
 * - pollJob()
 *
 * REÇOIT :
 * - queued
 * - running
 * - completed
 * - failed
 *
 * MODIFIE :
 * - titre du job
 * - identifiant court
 * - message d'état
 * - barre de progression
 *
 * PIPELINE :
 * - CHAT_JOB_STATUS
 */
function updateJobStatus(status) {
    if (
        !jobStatus
        || !jobStatusTitle
        || !jobStatusMessage
    ) {
        return;
    }

    jobStatus.hidden = false;

    if (
        jobStatusId
        && status.job_id
    ) {
        jobStatusId.textContent =
            "Job " + status.job_id.slice(0, 8);
    }

    if (status.state === "queued") {
        jobStatusTitle.textContent =
            "Job placé dans la file";

        if (status.queue_position) {
            jobStatusMessage.textContent =
                "Position estimée : "
                + status.queue_position
                + " sur "
                + Math.max(
                    status.queue_total,
                    status.queue_position
                )
                + ".";
        } else {
            jobStatusMessage.textContent =
                "En attente du worker.";
        }
    }

    if (status.state === "running") {
        jobStatusTitle.textContent =
            "Le worker traite la demande";

        jobStatusMessage.textContent =
            "Qwen génère la réponse. "
            + "La durée restante n'est "
            + "pas calculable précisément.";
    }

    if (status.state === "completed") {
        jobStatusTitle.textContent =
            "Réponse terminée";

        jobStatusMessage.textContent =
            "La réponse va être affichée.";
    }

    if (status.state === "failed") {
        jobStatusTitle.textContent =
            "Le job a échoué";

        jobStatusMessage.textContent =
            status.error || "Erreur inconnue.";
    }

    updateProgressBar(status);
}


/*
 * RÔLE :
 * Affiche une progression réelle lorsqu'elle existe
 * ou une animation indéterminée pendant la génération.
 *
 * APPELÉE PAR :
 * - updateJobStatus()
 *
 * UTILISE :
 * - apps/front/chat/static/chat/chat.css
 * - classe is-indeterminate
 *
 * PIPELINE :
 * - CHAT_JOB_STATUS
 */
function updateProgressBar(status) {
    if (
        !jobStatusProgress
        || !jobStatusProgressBar
    ) {
        return;
    }

    jobStatusProgressBar.classList.remove(
        "is-indeterminate"
    );

    if (
        Number.isInteger(
            status.progress_percent
        )
    ) {
        jobStatusProgress.hidden = false;

        jobStatusProgressBar.style.width =
            status.progress_percent + "%";

        return;
    }

    if (status.state === "running") {
        jobStatusProgress.hidden = false;
        jobStatusProgressBar.style.width = "35%";

        jobStatusProgressBar.classList.add(
            "is-indeterminate"
        );

        return;
    }

    jobStatusProgress.hidden = true;
}


/*
 * RÔLE :
 * Attend une seconde entre deux lectures du statut.
 *
 * APPELÉE PAR :
 * - pollJob()
 *
 * PIPELINE :
 * - CHAT_JOB_STATUS
 */
function waitOneSecond() {
    return new Promise((resolve) => {
        window.setTimeout(
            resolve,
            1000
        );
    });
}


/*
 * RÔLE :
 * Lit une réponse HTTP et vérifie
 * qu'elle contient bien du JSON.
 *
 * APPELÉE PAR :
 * - submitJob()
 * - pollJob()
 *
 * RETOURNE :
 * - Objet JSON valide
 *
 * ERREUR :
 * - Lance Error lorsque la réponse n'est pas du JSON.
 *
 * PIPELINES :
 * - CHAT_JOB_CREATE
 * - CHAT_JOB_STATUS
 */
async function readJsonResponse(response) {
    try {
        return await response.json();

    } catch {
        throw new Error(
            "Le serveur n'a pas retourné "
            + "une réponse JSON valide."
        );
    }
}


/*
 * RÔLE :
 * Interroge Django toutes les secondes
 * jusqu'à la fin du ticket.
 *
 * APPELÉE PAR :
 * - submitJob()
 * - resumeActiveJob()
 *
 * APPELLE :
 * - GET /jobs/<job_id>/
 * - apps/front/chat/views.py::job_status()
 * - readJsonResponse()
 * - updateJobStatus()
 *
 * ARRÊT :
 * - completed : recharge la page
 * - failed : lance une erreur
 *
 * PIPELINES :
 * - CHAT_JOB_STATUS
 * - CHAT_JOB_COMPLETE
 * - CHAT_JOB_FAILURE
 */
async function pollJob(statusUrl) {
    while (true) {
        await waitOneSecond();

        const response = await fetch(
            statusUrl,
            {
                method: "GET",
                headers: {
                    "Accept": "application/json",
                },
            }
        );

        const payload = await readJsonResponse(
            response
        );

        if (!response.ok) {
            throw new Error(
                payload.error
                || "Impossible de suivre le job."
            );
        }

        updateJobStatus(payload);

        if (
            payload.state === "completed"
            && payload.reload
        ) {
            window.location.reload();
            return;
        }

        if (payload.state === "failed") {
            throw new Error(
                payload.error
                || "Le worker a échoué."
            );
        }
    }
}


/*
 * RÔLE :
 * Envoie le formulaire à Django afin de créer
 * un nouveau ticket FastAPI/RabbitMQ.
 *
 * APPELÉE PAR :
 * - Événement submit du formulaire #chat-form
 *
 * APPELLE :
 * - POST /jobs/submit/
 * - apps/front/chat/views.py::submit_job()
 * - readJsonResponse()
 * - updateJobStatus()
 * - pollJob()
 *
 * PIPELINE :
 * - CHAT_JOB_CREATE
 * - CHAT_JOB_STATUS
 */
async function submitJob() {
    if (!form) {
        return;
    }

    clearError();

    /*
     * Les données sont construites avant le blocage du formulaire
     * pour garantir la présence du message et du jeton CSRF.
     */
    const formData = new FormData(form);

    setFormBusy(true);

    try {
        const response = await fetch(
            form.action,
            {
                method: "POST",
                body: formData,
                headers: {
                    "Accept": "application/json",
                },
            }
        );

        const payload = await readJsonResponse(
            response
        );

        if (!response.ok) {
            throw new Error(
                payload.error
                || "Impossible de créer le job."
            );
        }

        updateJobStatus(payload);

        await pollJob(
            payload.status_url
        );

    } catch (error) {
        showError(
            error instanceof Error
                ? error.message
                : "Erreur inattendue."
        );

        setFormBusy(false);
    }
}


/*
 * RÔLE :
 * Associe Commande + Entrée ou Contrôle + Entrée
 * à l'envoi du formulaire.
 *
 * APPELÉE PAR :
 * - initialiseChatPage()
 *
 * APPELLE :
 * - form.requestSubmit()
 *
 * PIPELINE :
 * - CHAT_JOB_CREATE
 */
function bindComposerShortcut() {
    if (
        !textarea
        || !form
    ) {
        return;
    }

    textarea.addEventListener(
        "keydown",
        (event) => {
            const usesShortcut =
                (
                    event.metaKey
                    || event.ctrlKey
                )
                && event.key === "Enter";

            if (!usesShortcut) {
                return;
            }

            event.preventDefault();
            form.requestSubmit();
        }
    );
}


/*
 * RÔLE :
 * Relie les boutons de suggestions
 * au champ de saisie.
 *
 * APPELÉE PAR :
 * - initialiseChatPage()
 *
 * APPELLE :
 * - resizeTextarea()
 *
 * PIPELINE :
 * - CHAT_PAGE_DISPLAY
 */
function bindSuggestionButtons() {
    document.querySelectorAll(
        "[data-question]"
    ).forEach((button) => {
        button.addEventListener(
            "click",
            () => {
                if (!textarea) {
                    return;
                }

                textarea.value =
                    button.dataset.question || "";

                resizeTextarea();
                textarea.focus();
            }
        );
    });
}


/*
 * RÔLE :
 * Reprend le polling lorsqu'un job était encore actif
 * avant le rechargement de la page.
 *
 * APPELÉE PAR :
 * - initialiseChatPage()
 *
 * LIT :
 * - data-active-job-status-url
 * - fourni par apps/front/chat/views.py::index()
 *
 * APPELLE :
 * - pollJob()
 * - setFormBusy()
 * - showError()
 *
 * PIPELINE :
 * - CHAT_JOB_STATUS
 */
function resumeActiveJob() {
    const activeStatusUrl = appRoot
        ? appRoot.dataset.activeJobStatusUrl
        : "";

    if (!activeStatusUrl) {
        return;
    }

    setFormBusy(true);

    pollJob(activeStatusUrl).catch(
        (error) => {
            showError(
                error instanceof Error
                    ? error.message
                    : "Erreur de suivi."
            );

            setFormBusy(false);
        }
    );
}


/*
 * RÔLE :
 * Initialise toutes les interactions de la page.
 *
 * APPELÉE PAR :
 * - Événement DOMContentLoaded
 *
 * APPELLE :
 * - resizeTextarea()
 * - scrollConversation()
 * - bindComposerShortcut()
 * - bindSuggestionButtons()
 * - resumeActiveJob()
 * - submitJob()
 *
 * PIPELINES :
 * - CHAT_PAGE_DISPLAY
 * - CHAT_JOB_CREATE
 * - CHAT_JOB_STATUS
 */
function initialiseChatPage() {
    resizeTextarea();
    scrollConversation();

    bindComposerShortcut();
    bindSuggestionButtons();
    resumeActiveJob();

    if (textarea) {
        textarea.addEventListener(
            "input",
            resizeTextarea
        );
    }

    if (form) {
        form.addEventListener(
            "submit",
            (event) => {
                event.preventDefault();
                submitJob();
            }
        );
    }
}


/*
 * Point d'entrée JavaScript de la page.
 */
document.addEventListener(
    "DOMContentLoaded",
    initialiseChatPage
);