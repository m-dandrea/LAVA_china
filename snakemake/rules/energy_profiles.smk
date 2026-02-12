rule energy_profiles:
    output:
        touch(logpath("{region}", "energy_profiles_{technology}_{weather_year}_{scenario}.done"))
    params:
        method="snakemake", 
        scenario=scenario
    shell:
        (
            "python energy_profiles.py --region {wildcards.region} "
            "--technology {wildcards.technology} "
            "--weather_year {wildcards.weather_year} "
            "--method {params.method} "
            "--scenario {params.scenario} "
        )