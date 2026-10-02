/**
 * Modal dialog handler using native <dialog> element.
 * Delegated click listeners for open/close.
 */
(function () {
    "use strict";

    function initModals() {
        // Open modal
        document.addEventListener("click", function (e) {
            var openBtn = e.target.closest("[data-modal-open]");
            if (openBtn) {
                var modalId = openBtn.dataset.modalOpen;
                var modal = document.getElementById(modalId);
                if (modal) {
                    modal.showModal();
                }
            }
        });

        // Close modal
        document.addEventListener("click", function (e) {
            var closeBtn = e.target.closest("[data-modal-close]");
            if (closeBtn) {
                var modal = closeBtn.closest("dialog");
                if (modal) {
                    modal.close();
                }
            }
        });

        // Close on backdrop click
        document.addEventListener("click", function (e) {
            var modal = e.target.closest("dialog");
            if (modal && e.target === modal) {
                modal.close();
            }
        });

        // Close on Escape key
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape") {
                var openModal = document.querySelector("dialog[open]");
                if (openModal) {
                    openModal.close();
                }
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initModals);
    } else {
        initModals();
    }
})();