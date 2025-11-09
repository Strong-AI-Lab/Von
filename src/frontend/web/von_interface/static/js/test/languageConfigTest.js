/**
 * Test file to verify language configuration functionality
 */

// Import the language configuration
import { 
  SUPPORTED_LANGUAGES, 
  DEFAULT_LANGUAGE, 
  LANGUAGE_DISPLAY_NAMES,
  createLanguageOptions,
  populateLanguageSelect,
  getLanguageDisplayName,
  isLanguageSupported
} from '../languageConfig.js';

// Test function to verify language configuration
function testLanguageConfig() {
  console.log('Testing language configuration...');
  
  // Test SUPPORTED_LANGUAGES
  console.log('Supported languages:', SUPPORTED_LANGUAGES);
  console.assert(Array.isArray(SUPPORTED_LANGUAGES), 'SUPPORTED_LANGUAGES should be an array');
  console.assert(SUPPORTED_LANGUAGES.length > 0, 'SUPPORTED_LANGUAGES should not be empty');
  
  // Test DEFAULT_LANGUAGE
  console.log('Default language:', DEFAULT_LANGUAGE);
  console.assert(typeof DEFAULT_LANGUAGE === 'string', 'DEFAULT_LANGUAGE should be a string');
  console.assert(DEFAULT_LANGUAGE === 'en-NZ', 'DEFAULT_LANGUAGE should be en-NZ');
  
  // Test LANGUAGE_DISPLAY_NAMES
  console.log('Language display names:', LANGUAGE_DISPLAY_NAMES);
  console.assert(typeof LANGUAGE_DISPLAY_NAMES === 'object', 'LANGUAGE_DISPLAY_NAMES should be an object');
  
  // Test createLanguageOptions
  const options = createLanguageOptions('ja');
  console.log('Created options for ja:', options);
  console.assert(Array.isArray(options), 'createLanguageOptions should return an array');
  console.assert(options.length === SUPPORTED_LANGUAGES.length, 'Should create options for all supported languages');
  
  // Test getLanguageDisplayName
  console.assert(getLanguageDisplayName('en-NZ') === 'English (NZ)', 'Should return correct display name for en-NZ');
  console.assert(getLanguageDisplayName('ja') === 'Japanese', 'Should return correct display name for ja');
  console.assert(getLanguageDisplayName('unknown') === 'unknown', 'Should return code for unknown languages');
  
  // Test isLanguageSupported
  console.assert(isLanguageSupported('en-NZ') === true, 'Should recognize supported language');
  console.assert(isLanguageSupported('ja') === true, 'Should recognize Japanese as supported');
  console.assert(isLanguageSupported('unknown') === false, 'Should not recognize unsupported language');
  
  console.log('All language configuration tests passed!');
}

// Test HTML element creation
function testDOMInteraction() {
  // Create a test select element
  const select = document.createElement('select');
  select.id = 'testLanguageSelect';
  
  // Test populateLanguageSelect
  populateLanguageSelect(select, 'zh');
  
  console.log('Populated select element:', select);
  console.assert(select.children.length === SUPPORTED_LANGUAGES.length, 'Should populate all language options');
  
  // Check if Chinese is selected
  const selectedOption = select.querySelector('option[selected]');
  console.assert(selectedOption && selectedOption.value === 'zh', 'Should select Chinese option');
  
  console.log('DOM interaction tests passed!');
}

// Run tests when DOM is ready
if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => {
    testLanguageConfig();
    testDOMInteraction();
  });
} else {
  // Running in Node.js environment
  testLanguageConfig();
}
