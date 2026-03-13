def time_to_val(minutes, seconds, ms, rate=3000):
    total_seconds = (minutes * 60) + seconds + (ms / 1000)
    return int(total_seconds * rate)


# -1s and 900 for brzone standard devation
result = time_to_val(1, 55, 428- 100)
print(result) 