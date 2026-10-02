/**
 * Infinite scroll for recommendations and auto-apply queue pages.
 * Watches for a sentinel element and fetches the next page when it enters viewport.
 */
(function () {
    "use strict";

    function initInfiniteScroll() {
        var sentinel = document.querySelector("[data-scroll-sentinel]");
        if (!sentinel) {
            return;
        }

        var nav = document.querySelector("#rec-pagination-nav, #queue-pagination-nav");
        var listContainer = document.querySelector("#recommendations-list, #auto-apply-queue-list");

        if (!listContainer) {
            return;
        }

        var observer = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (entry) {
                    if (entry.isIntersecting) {
                        loadNextPage(sentinel, listContainer, nav, observer);
                    }
                });
            },
            { rootMargin: "200px" }
        );

        observer.observe(sentinel);

        // Hide pagination nav on successful setup (progressive enhancement)
        if (nav) {
            nav.style.display = "none";
        }
    }

    function loadNextPage(sentinel, listContainer, nav, observer) {
        var nextUrl = sentinel.dataset.nextUrl;
        if (!nextUrl) {
            return;
        }

        // Prevent double-fetch
        if (sentinel.dataset.loading === "true") {
            return;
        }
        sentinel.dataset.loading = "true";

        fetch(nextUrl, {
            headers: { "X-Requested-With": "XMLHttpRequest" },
        })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("Network response was not ok");
                }
                return response.text();
            })
            .then(function (html) {
                var tempDiv = document.createElement("div");
                tempDiv.innerHTML = html.trim();

                // Insert new cards before the old sentinel
                var newSentinel = tempDiv.querySelector("[data-scroll-sentinel]");
                var newCards = tempDiv.querySelectorAll(":scope > *:not([data-scroll-sentinel])");

                newCards.forEach(function (node) {
                    sentinel.parentNode.insertBefore(node, sentinel);
                });

                // Replace sentinel with new one (or remove if no more pages)
                if (newSentinel) {
                    sentinel.parentNode.replaceChild(newSentinel, sentinel);
                    observer.unobserve(sentinel);
                    observer.observe(newSentinel);
                    sentinel = newSentinel;
                } else {
                    observer.unobserve(sentinel);
                    sentinel.parentNode.removeChild(sentinel);
                }
            })
            .catch(function (err) {
                console.error("Infinite scroll fetch failed:", err);
            })
            .finally(function () {
                sentinel.dataset.loading = "false";
            });
    }

    // Initialize when DOM is ready
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initInfiniteScroll);
    } else {
        initInfiniteScroll();
    }
})();