targetScope = 'resourceGroup'

param location string = resourceGroup().location
param appName string = 'signal-capture'
@description('Existing private ACR name in this resource group')
param registryName string
@description('Immutable image tag already pushed to the registry, e.g. signal-capture:20260911-1')
param imageTag string
@secure()
param discordBotToken string
@secure()
param mt5Password string
param mt5Login string
param mt5Server string

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${appName}-identity'
  location: location
}

resource pullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'AcrPull')
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'sig${uniqueString(resourceGroup().id, appName)}'
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${appName}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: '${appName}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      // No public ingress. Discord and the broker are outbound connections.
      registries: [{ server: registry.properties.loginServer, identity: identity.id }]
      secrets: [
        { name: 'discord-token', value: discordBotToken }
        { name: 'mt5-password', value: mt5Password }
        {
          name: 'storage-connection'
          value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
        }
      ]
    }
    template: {
      terminationGracePeriodSeconds: 90
      scale: { minReplicas: 1, maxReplicas: 1 }
      containers: [
        {
          name: 'capture'
          image: '${registry.properties.loginServer}/${imageTag}'
          resources: { cpu: 2, memory: '4Gi' }
          env: [
            { name: 'DISCORD_BOT_TOKEN', secretRef: 'discord-token' }
            { name: 'MT5_PASSWORD', secretRef: 'mt5-password' }
            { name: 'MT5_LOGIN', value: mt5Login }
            { name: 'MT5_SERVER', value: mt5Server }
            { name: 'AZURE_STORAGE_CONNECTION_STRING', secretRef: 'storage-connection' }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: { path: '/health/live', port: 8080 }
              periodSeconds: 10
              failureThreshold: 30
              timeoutSeconds: 5
            }
            {
              type: 'Liveness'
              httpGet: { path: '/health/live', port: 8080 }
              periodSeconds: 30
              failureThreshold: 3
              timeoutSeconds: 5
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health/ready', port: 8080 }
              periodSeconds: 10
              failureThreshold: 3
              timeoutSeconds: 5
            }
          ]
        }
      ]
    }
  }
  dependsOn: [pullRole]
}

output containerAppName string = app.name
output storageAccountName string = storage.name
output logsWorkspaceName string = logs.name
