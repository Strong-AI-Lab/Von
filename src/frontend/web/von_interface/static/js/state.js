// Global state management for Von application

// Concept Tab State
export let currentConceptType = null;
export let defaultSelectedConceptType = "#V#person";
export let currentlySelectedConceptId = null;
export let currentInteractionId = null;
export let selectedConceptOriginalName = null;

// Vontology State  
export let currentVontologyPath = null;
export let currentVontologyNode = null;
export let currentVontologyNodeId = null;
export let currentSelectedVontologyPath = null;
export let currentHighlightedVontologyNode = null;
export let vontologyTreeData = null;
// The currently selected Vontology TYPE concept_id (always a #V# identifier).
// This is intentionally decoupled from currentConceptType (which may change
// when switching tabs) so that Vontology actions (like "Create under XXX")
// always target the explicitly selected type in the tree.
export let selectedVontologyConceptId = null;

// State getters
export function getCurrentConceptType() {
  return currentConceptType || defaultSelectedConceptType;
}

export function getCurrentlySelectedConceptId() {
  return currentlySelectedConceptId;
}

export function getSelectedConceptOriginalName() {
    return selectedConceptOriginalName;
}

export function getSelectedVontologyConceptId() {
  return selectedVontologyConceptId;
}

// State setters
export function setCurrentConceptType(type) {
  currentConceptType = type;
  console.log(`State: currentConceptType set to ${type}`);
}

export function setCurrentlySelectedConceptId(id) {
  currentlySelectedConceptId = id;
  console.log(`State: currentlySelectedConceptId set to ${id}`);
}

export function setCurrentInteractionId(id) {
  currentInteractionId = id;
  console.log(`State: currentInteractionId set to ${id}`);
}

export function setSelectedConceptOriginalName(name) {
  selectedConceptOriginalName = name;
  console.log(`State: selectedConceptOriginalName set to ${name}`);
}

export function setCurrentVontologyPath(path) {
  currentVontologyPath = path;
  console.log(`State: currentVontologyPath set to ${path}`);
}

export function setCurrentVontologyNode(node) {
  currentVontologyNode = node;
  console.log(`State: currentVontologyNode set to`, node);
}

export function setCurrentVontologyNodeId(id) {
  currentVontologyNodeId = id;
  console.log(`State: currentVontologyNodeId set to ${id}`);
}

export function setVontologyTreeData(data) {
  vontologyTreeData = data;
  console.log("State: vontologyTreeData updated");
}

export function setSelectedVontologyConceptId(id) {
  selectedVontologyConceptId = id;
  console.log(`State: selectedVontologyConceptId set to ${id}`);
}

// Helper function to find a node in the tree by its ID (_id or concept_id)
function findNodeById(nodes, id) {
  for (const node of nodes) {
    if (node.mongo_id === id || node.id === id) {
      return node;
    }
    if (node.children && node.children.length > 0) {
      const found = findNodeById(node.children, id);
      if (found) return found;
    }
  }
  return null;
}

export function getConceptTypeDisplayNames(conceptType) {
  if (!conceptType) {
    conceptType = defaultSelectedConceptType;
  }

  // If we have tree data, try to find the node by ID to get its name
  if (vontologyTreeData && vontologyTreeData.tree) {
    const node = findNodeById(vontologyTreeData.tree, conceptType);
    if (node && node.name) {
      // Basic pluralization (add 's'), can be improved
      const pluralName = node.name.endsWith('s') ? node.name : `${node.name}s`;
      return {
        singular: node.name,
        plural: pluralName,
        apiType: conceptType // The original ID is the correct API type
      };
    }
  }

  // Fallback logic for when tree data is not available or node not found
  const typeName = conceptType.startsWith('#V#') ? conceptType.substring(3) : conceptType;
  const singular = typeName.charAt(0).toUpperCase() + typeName.slice(1);
  const plural = singular.endsWith('s') ? singular : `${singular}s`;

  return {
    singular: singular,
    plural: plural,
    apiType: conceptType
  };
}
