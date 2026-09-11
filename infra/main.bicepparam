using './main.bicep'

param registryName = readEnvironmentVariable('ACR_NAME')
param imageTag = readEnvironmentVariable('IMAGE_TAG')
param discordBotToken = readEnvironmentVariable('DISCORD_BOT_TOKEN')
param mt5Password = readEnvironmentVariable('MT5_PASSWORD')
param mt5Login = readEnvironmentVariable('MT5_LOGIN')
param mt5Server = readEnvironmentVariable('MT5_SERVER')
