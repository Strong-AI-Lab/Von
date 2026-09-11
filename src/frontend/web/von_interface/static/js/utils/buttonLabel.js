// Preserve decorative icons while updating the accessible action/state label.
export function setButtonLabel(button, label) {
    const target = button.querySelector('.button-label') || button;
    target.textContent = label;
}
