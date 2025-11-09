/**
 * Shared language configuration for the Von application
 * This ensures consistent language options across the UI
 */

/**
 * Available languages with their display names
 * @type {Array<{code: string, name: string}>}
 */
export const SUPPORTED_LANGUAGES = [
  { code: 'en-NZ', name: 'English (New Zealand)' },
  { code: 'mi', name: 'Māori' },
  { code: 'zh', name: 'Chinese' },
  { code: 'es', name: 'Spanish' },
  { code: 'fr', name: 'French' },
  { code: 'ja', name: 'Japanese' },
  { code: 'ko', name: 'Korean' }
];

/**
 * Default language code
 */
export const DEFAULT_LANGUAGE = 'en-NZ';

/**
 * Language display names for the footer indicator
 * Includes additional variants for display purposes
 */
export const LANGUAGE_DISPLAY_NAMES = {
  'en-NZ': 'English (NZ)',
  'en-US': 'English (US)',
  'en-GB': 'English (UK)', 
  'mi': 'Māori',
  'zh': 'Chinese',
  'es': 'Spanish',
  'fr': 'French',
  'de': 'German',
  'ja': 'Japanese',
  'ko': 'Korean'
};

/**
 * Create language option elements for select dropdowns
 * @param {string} selectedValue - The currently selected language code
 * @returns {Array<HTMLOptionElement>} Array of option elements
 */
export function createLanguageOptions(selectedValue = DEFAULT_LANGUAGE) {
  return SUPPORTED_LANGUAGES.map(lang => {
    const option = document.createElement('option');
    option.value = lang.code;
    option.textContent = lang.name;
    if (lang.code === selectedValue) {
      option.selected = true;
    }
    return option;
  });
}

/**
 * Populate a select element with language options
 * @param {HTMLSelectElement} selectElement - The select element to populate
 * @param {string} selectedValue - The currently selected language code
 */
export function populateLanguageSelect(selectElement, selectedValue = DEFAULT_LANGUAGE) {
  if (!selectElement) {
    console.warn('Language select element not found');
    return;
  }
  
  // Clear existing options
  selectElement.innerHTML = '';
  
  // Add language options
  const options = createLanguageOptions(selectedValue);
  options.forEach(option => selectElement.appendChild(option));
}

/**
 * Get the display name for a language code
 * @param {string} languageCode - The language code
 * @returns {string} The display name or the code if not found
 */
export function getLanguageDisplayName(languageCode) {
  return LANGUAGE_DISPLAY_NAMES[languageCode] || languageCode;
}

/**
 * Check if a language code is supported
 * @param {string} languageCode - The language code to check
 * @returns {boolean} True if the language is supported
 */
export function isLanguageSupported(languageCode) {
  return SUPPORTED_LANGUAGES.some(lang => lang.code === languageCode);
}
