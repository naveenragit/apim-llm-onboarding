// One-time prerequisite for Activity 1: let the APIM managed identity call the model.
// Deploy into the resource group that contains the Azure OpenAI / Foundry resource.
targetScope = 'resourceGroup'

@description('Name of the Azure OpenAI or Microsoft Foundry account that hosts the model deployment.')
param aiAccountName string

@description('Name of the API Management instance (system-assigned identity must be enabled).')
param apimName string

@description('Resource group of the API Management instance.')
param apimResourceGroup string = resourceGroup().name

// Built-in role: Cognitive Services OpenAI User
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource aiAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: aiAccountName
}

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
  scope: resourceGroup(apimResourceGroup)
}

resource apimModelAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiAccount.id, apim.id, openAiUserRoleId)
  scope: aiAccount
  properties: {
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
}
