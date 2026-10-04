"use strict";

const form = document.getElementById("loginForm");
const emailInput = document.getElementById("email");
const passwordInput = document.getElementById("password");
const togglePassword = document.getElementById("togglePassword");
const submitButton = document.getElementById("submitButton");
const errorMessage = document.getElementById("loginError");

togglePassword.addEventListener("click", () => {
  const showPassword = passwordInput.type === "password";
  passwordInput.type = showPassword ? "text" : "password";
  togglePassword.textContent = showPassword ? "Hide" : "Show";
  togglePassword.setAttribute("aria-label", `${showPassword ? "Hide" : "Show"} password`);
  togglePassword.setAttribute("aria-pressed", String(showPassword));
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorMessage.textContent = "";
  if (!form.reportValidity()) return;

  submitButton.disabled = true;
  submitButton.classList.add("loading");
  submitButton.setAttribute("aria-busy", "true");
  submitButton.querySelector(".button-label").textContent = "Signing in";
  try {
    const response = await fetch("/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ email: emailInput.value.trim(), password: passwordInput.value }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Sign in failed. Please try again.");
    window.location.replace("/");
  } catch (error) {
    errorMessage.textContent = error.message === "Failed to fetch"
      ? "Unable to reach Hirevia. Check your connection and try again."
      : error.message;
    passwordInput.focus();
  } finally {
    submitButton.disabled = false;
    submitButton.classList.remove("loading");
    submitButton.removeAttribute("aria-busy");
    submitButton.querySelector(".button-label").textContent = "Sign in";
  }
});