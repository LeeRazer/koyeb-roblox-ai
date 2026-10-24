--[[
	GlobalAIDirectorBridge.luau (reference client)

	Use only from a server ModuleScript. Configure these attributes on the script:
	- ServerURL: the Koyeb HTTPS origin
	- APIKey: the same secret as AI_API_KEY on Koyeb

	Never place this ModuleScript or its APIKey in ReplicatedStorage.
]]

local HttpService = game:GetService("HttpService")
local RunService = game:GetService("RunService")

assert(RunService:IsServer(), "GlobalAIDirectorBridge must run on the server")

local Bridge = {}

local SNAPSHOT_PATH = "/api/v2/snapshot"
local EVENTS_PATH = "/api/v2/events/batch"
local CLIENT_VERSION = "roblox-bridge-2"
local FETCH_INTERVAL = 300
local FLUSH_INTERVAL = 8
local MAX_BATCH = 100

local DEFAULT_MODIFIERS = {
	horde_multiplier = 1.0,
	special_cap_bonus = 0,
	stress_accumulation_rate = 1.0,
	interval_speed = 1.0,
}

local snapshot = {
	schema_version = 2,
	version = "local-default",
	timestamp = 0,
	director_modifiers = DEFAULT_MODIFIERS,
	zombies = {},
}
local queue = {}
local started = false
local fetchInFlight = false

local function deepCopy(value, seen)
	if type(value) ~= "table" then return value end
	seen = seen or {}
	if seen[value] then return seen[value] end
	local result = {}
	seen[value] = result
	for key, child in pairs(value) do
		result[deepCopy(key, seen)] = deepCopy(child, seen)
	end
	return result
end

local function serverURL()
	local value = script:GetAttribute("ServerURL")
	assert(type(value) == "string" and string.match(value, "^https://"), "Set a valid ServerURL attribute")
	return string.gsub(value, "/+$", "")
end

local function headers()
	local apiKey = script:GetAttribute("APIKey")
	local result = {
		["Content-Type"] = "application/json",
		["X-AI-Client-Schema"] = CLIENT_VERSION,
		["X-Roblox-Universe-Id"] = tostring(game.GameId),
		["X-Roblox-Place-Id"] = tostring(game.PlaceId),
		["X-Roblox-Job-Id"] = game.JobId ~= "" and game.JobId or "studio",
	}
	if type(apiKey) == "string" and apiKey ~= "" then
		result["X-API-Key"] = apiKey
	end
	return result
end

local function request(method, path, body)
	local options = {
		Url = serverURL() .. path,
		Method = method,
		Headers = headers(),
	}
	if body ~= nil then
		options.Body = HttpService:JSONEncode(body)
	end
	return HttpService:RequestAsync(options)
end

local function isSuccessful(response)
	return response.Success and response.StatusCode >= 200 and response.StatusCode < 300
end

function Bridge.FetchSnapshot()
	if fetchInFlight then return false, "fetch already running" end
	fetchInFlight = true
	local ok, response = pcall(request, "GET", SNAPSHOT_PATH, nil)
	fetchInFlight = false
	if not ok or not isSuccessful(response) then
		return false, ok and ("HTTP " .. tostring(response.StatusCode)) or tostring(response)
	end

	local decodedOk, data = pcall(HttpService.JSONDecode, HttpService, response.Body)
	if not decodedOk or type(data) ~= "table" or type(data.zombies) ~= "table" then
		return false, "invalid snapshot"
	end
	if type(data.version) ~= "string" or type(data.timestamp) ~= "number" then
		return false, "snapshot without version metadata"
	end
	if data.timestamp < snapshot.timestamp then
		return false, "older snapshot ignored"
	end

	snapshot = deepCopy(data)
	script:SetAttribute("AIConfigVersion", snapshot.version)
	script:SetAttribute("AIConfigTimestamp", snapshot.timestamp)
	return true
end

function Bridge.GetSnapshot()
	return deepCopy(snapshot)
end

function Bridge.GetModifiers()
	return deepCopy(snapshot.director_modifiers or DEFAULT_MODIFIERS)
end

function Bridge.GetZombieWeights(enemyType)
	local weights = snapshot.zombies and snapshot.zombies[enemyType]
	return weights and deepCopy(weights) or nil
end

function Bridge.QueueActionReward(enemyType, actionName, reward, playerDashed, damageDealt, context)
	if type(enemyType) ~= "string" or enemyType == "" or #enemyType > 64 then return false end
	if type(actionName) ~= "string" or actionName == "" or #actionName > 64 then return false end
	if type(reward) ~= "number" or reward ~= reward then return false end
	if #queue >= 500 then return false end

	context = type(context) == "table" and context or {}
	table.insert(queue, {
		event_id = HttpService:GenerateGUID(false),
		enemy_type = enemyType,
		action_name = actionName,
		reward = math.clamp(reward, -5, 5),
		player_dashed = playerDashed == true,
		player_health_lost = math.clamp(tonumber(damageDealt) or 0, 0, 10000),
		occurred_at = os.time(),
		difficulty = context.difficulty,
		room_number = context.room_number,
	})
	script:SetAttribute("AIPendingReports", #queue)
	return true
end

function Bridge.Flush()
	if #queue == 0 then return true end

	local count = math.min(MAX_BATCH, #queue)
	local events = table.create(count)
	for index = 1, count do
		events[index] = queue[index]
	end
	local payload = {
		client_version = CLIENT_VERSION,
		universe_id = tostring(game.GameId),
		place_id = tostring(game.PlaceId),
		job_id = game.JobId ~= "" and game.JobId or "studio",
		events = events,
	}

	local ok, response = pcall(request, "POST", EVENTS_PATH, payload)
	if not ok or not isSuccessful(response) then
		return false, ok and ("HTTP " .. tostring(response.StatusCode)) or tostring(response)
	end
	for _ = 1, count do
		table.remove(queue, 1)
	end
	script:SetAttribute("AIPendingReports", #queue)
	return true
end

function Bridge.Start()
	if started then return end
	started = true
	task.spawn(function()
		while started do
			local ok, err = Bridge.FetchSnapshot()
			if not ok then warn("[GlobalAIDirectorBridge] snapshot: " .. tostring(err)) end
			task.wait(FETCH_INTERVAL + math.random(0, 30))
		end
	end)
	task.spawn(function()
		while started do
			task.wait(FLUSH_INTERVAL)
			local ok, err = Bridge.Flush()
			if not ok then warn("[GlobalAIDirectorBridge] telemetry: " .. tostring(err)) end
		end
	end)
end

function Bridge.Stop()
	started = false
end

return Bridge
