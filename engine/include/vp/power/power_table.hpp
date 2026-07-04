/*
 * Copyright (C) 2020 GreenWaves Technologies, SAS, ETH Zurich and
 *                    University of Bologna
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/*
 * Authors: Germain Haugou, GreenWaves Technologies (germain.haugou@greenwaves-technologies.com)
 */

#pragma once



namespace vp
{
    class PowerLinearTempTable;
    class PowerLinearVoltTable;
    class PowerLinearFreqTable;

    /**
     * @brief One point of a linear power table
     *
     * Gives the power characteristic value at one (temperature, voltage, frequency)
     * operating point. This is the struct-based equivalent of one leaf of the JSON
     * "values" tree.
     */
    struct PowerTableEntry
    {
        double temp;   // Temperature in celsius
        double volt;   // Voltage in V
        double freq;   // Frequency in Hz. A negative value means "any", i.e. the value
                       // does not depend on frequency.
        double value;  // Power characteristic value, in pJ for energy quanta or in W
                       // for background and leakage power.
    };

    /**
     * @brief Struct-based description of a power source
     *
     * Equivalent of the JSON power source configuration, for models which get their
     * power tables through their compiled config struct instead of the JSON config.
     * The pointed entries are copied when the power source is initialized, so they
     * can point to temporaries.
     */
    struct PowerSourceTable
    {
        const char *dynamic_unit = "";           // "pJ" for a per-event energy quantum,
                                                 // "W" for background power, "" when the
                                                 // source has no dynamic part.
        const PowerTableEntry *dynamic = NULL;   // Dynamic table points
        size_t dynamic_count = 0;
        const PowerTableEntry *leakage = NULL;   // Leakage table points, always in W
        size_t leakage_count = 0;
    };

    /**
     * @brief Power values of a power characteristic
     * 
     * This manages the actual power value of a power characteristic, depending on current
     * temperature, voltage and frequency.
     * The value is interpolated, based on power numbers at various voltages, temperatures and frequencies.
     */
    class PowerLinearTable
    {
    public:
        /**
         * @brief Construct a new PowerLinearTable object
         * 
         * @param config JSON config giving all the power numbers at various frequencies, temperatures an voltages
         */
        PowerLinearTable(js::Config *config);

        /**
         * @brief Construct a new PowerLinearTable object from a flat entry list
         *
         * The entries are grouped by temperature, then voltage, to build the same
         * table hierarchy as the JSON constructor. Entries are copied.
         *
         * @param entries Flat list of table points
         * @param count   Number of points
         */
        PowerLinearTable(const PowerTableEntry *entries, size_t count);

        /**
         * @brief Get the power value at specified temperature, voltage and frequency
         * 
         * The value is interpolated from the tables extracted from JSON tables.
         * 
         * @param temp         Temperature
         * @param volt         Voltage
         * @param frequency    Frequency
         * @return double      The estimated power value
         */
        double get(double temp, double volt, double frequency);

    private:
        // Vector of power tables at supported temperatures
        std::vector<PowerLinearTempTable *> temp_tables;
    };

    /**
     * @brief Power values of a power characteristic at a given temperature
     */
    class PowerLinearTempTable
    {
    public:
        /**
         * @brief Construct a new PowerLinearTempTable object
         * 
         * @param temp   Temperature for which this table is valid
         * @param config JSON config containing the power numbers
         */
        PowerLinearTempTable(double temp, js::Config *config);

        /**
         * @brief Construct a new PowerLinearTempTable object from a flat entry list
         *
         * @param temp    Temperature for which this table is valid
         * @param entries Table points, all at the given temperature
         * @param count   Number of points
         */
        PowerLinearTempTable(double temp, const PowerTableEntry *entries, size_t count);

        /**
         * @brief Get the power value at specified voltage and frequency
         * 
         * The value is interpolated from the tables extracted from JSON tables.
         * These tables contains the power values at different voltages and frequencies
         * at the temperature for which this class has been created.
         * 
         * @param volt         Voltage
         * @param frequency    Frequency
         * @return double      The estimated power value
         */
        double get(double volt, double frequency);

        /**
         * @brief Get temperature for which this class has been created
         * 
         * @return double Temperature
         */
        inline double get_temp() { return this->temp; }

    private:
        std::vector<PowerLinearVoltTable *> volt_tables;   // Power tables at various voltages
        double temp;           // Temperature for which this table has been created
    };

    /**
     * @brief Power values of a power characteristic at a given temperature and voltage
     */
    class PowerLinearVoltTable
    {
    public:
        /**
         * @brief Construct a new PowerLinearVoltTable object
         * 
         * @param volt      Voltage for which the power numbers are defined.
         * @param config    JSON config giving the power numbers for various frequencies at the specified voltage
         */
        PowerLinearVoltTable(double volt, js::Config *config);

        /**
         * @brief Construct a new PowerLinearVoltTable object from a flat entry list
         *
         * A negative frequency in an entry makes the value frequency-independent.
         *
         * @param volt    Voltage for which this table is valid
         * @param entries Table points, all at the given voltage
         * @param count   Number of points
         */
        PowerLinearVoltTable(double volt, const PowerTableEntry *entries, size_t count);

        /**
         * @brief Get the power number at the specified frequency
         * 
         * The value is interpolated from the tables extracted from JSON tables.
         * These tables contains the power values at different frequencies
         * at the voltage and temperature for which this class has been created.
         * 
         * @param frequency Frequency at which the power number should be given
         * @return double 
         */
        double get(double frequency);

        /**
         * @brief Get voltage for which this class has been created
         * 
         * @return double Voltage
         */
        inline double get_volt() { return this->volt; }

    private:
        std::vector<PowerLinearFreqTable *> freq_tables;   // Power tables at various frequencies
        PowerLinearFreqTable *any=NULL;
        double volt;     // Voltage for which this class has been instantiated
    };

    class PowerLinearFreqTable
    {
    public:
        PowerLinearFreqTable(double freq, js::Config *config);

        PowerLinearFreqTable(double freq, double value) : freq(freq), value(value) {}

        inline double get_freq() { return this->freq; }
        inline double get() { return this->value; }

    private:
        double freq;
        double value;
    };
};

